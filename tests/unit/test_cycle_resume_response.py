"""`gauntlet resume --response` for parked adversarial_cycle steps (FR-10.4/10.5).

A reviewer-surfaced cycle escalation (upstream invalidation, max_rounds with open
blockers, or a triage escalation no agent could settle) parks the cycle — not the
builder. Before this feature `--response` rejected the cycle step type, deadlocking
any such run. These tests cover the three pieces of the fix:

1. the cycle stamps ``parked_reason = cycle_escalation`` on those parks;
2. ``RunManager._plan_response_action`` accepts ``--response`` for a parked cycle
   (and requires it on a cycle-escalation park, like a conflict park); and
3. the recorded decision is injected into the cycle's reviewer/triager on the
   next re-drive so they re-evaluate the parked finding instead of re-deriving it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from gauntlet.engine import manifest as M
from gauntlet.engine.config import RunConfig
from gauntlet.engine.cycle import _human_decision_block, wrap_as_data
from gauntlet.engine.manifest import (
    HumanResponse,
    Manifest,
    PipelineRef,
    StepRecord,
)
from gauntlet.engine.orchestrator import Orchestrator
from gauntlet.engine.pipeline import Pipeline
from gauntlet.engine.run import RunManager

# Reuse the scripted-cycle harness pieces from the sibling cycle test.
from test_cycle import (
    BASE_CONFIG,
    CONFIRM,
    CV,
    F,
    REVIEW,
    SeqAdapter,
    V,
    cycle_repo,  # noqa: F401  (pytest fixture)
    cycle_step,
    writer,
)

REPO = Path(__file__).resolve().parents[2]


def _response(text: str, *, ordinal: int = 1, state: str = "pending") -> HumanResponse:
    return HumanResponse(
        response_id=f"cycle-resp-{ordinal}",
        response_text=text,
        timestamp="2026-06-24T00:00:00+00:00",
        user="op@example.com",
        response_attempt=ordinal,
        state=state,
    )


def _drive_with_record(repo, adapters, cycle_record, *, step_extra=None):
    """Drive a single-cycle pipeline against a manifest pre-seeded with
    ``cycle_record`` (so a prior ``--response`` history is in place)."""
    pipeline = Pipeline.model_validate({
        "name": "demo", "version": 1,
        "stages": [{"id": "s", "steps": [cycle_step(**(step_extra or {}))]}],
    })
    cfg = RunConfig.model_validate(BASE_CONFIG)
    man = Manifest(run_id="r", slug="demo", branch="b", base_branch="main",
                   pipeline=PipelineRef(name="demo", version=1, hash="h"))
    man.steps.append(cycle_record)
    orch = Orchestrator(
        repo_root=repo, run_dir=repo / "runs" / "demo" / "run-1",
        artifact_root=repo, config=cfg, pipeline=pipeline, manifest=man,
        adapter_factory=lambda n: adapters[n],
    )
    return orch.drive(), man


# --- reject re-drives the upstream cycle with the note (report 2nd trigger) -----
def test_reject_gate_redrives_upstream_cycle_with_note(cycle_repo):
    """A rejected gate downstream of an adversarial_cycle injects the rejection
    note into that cycle as a new round — it does NOT terminally fail the run
    (matches the operator playbook).

    #79 changed the terminus of THIS script: the scripted reviewer ignores the
    injected note and re-converges with zero findings while prd.md stays
    byte-identical to the rejection checkpoint — the exact vacuous convergence
    that used to re-park the gate over an unrevised artifact. The engine now
    fails closed: the cycle parks for response instead of reporting DONE."""
    pipeline = Pipeline.model_validate({
        "name": "demo", "version": 1,
        "stages": [{"id": "s", "steps": [
            cycle_step(),
            {"id": "gate", "type": "human_gate"},
        ]}],
    })
    cfg = RunConfig.model_validate(BASE_CONFIG)
    man = Manifest(run_id="r", slug="demo", branch="b", base_branch="main",
                   pipeline=PipelineRef(name="demo", version=1, hash="h"))
    adapters = {
        "reviewer": SeqAdapter(REVIEW(), REVIEW()),  # converge, then converge on re-drive
        "triage": SeqAdapter(),
        "builder": SeqAdapter(),
    }
    orch = Orchestrator(
        repo_root=cycle_repo, run_dir=cycle_repo / "runs" / "demo" / "run-1",
        artifact_root=cycle_repo, config=cfg, pipeline=pipeline, manifest=man,
        adapter_factory=lambda n: adapters[n],
    )
    assert orch.drive() == M.RUN_PARKED
    assert man.record("gate").status == M.PARKED
    assert man.record("cycle").status == M.DONE

    status = orch.reject_gate(
        "gate", notes="tighten the threat-model section", user="op@example.com"
    )
    # Re-driven, not terminally failed — but the re-drive converged with zero
    # findings on a byte-identical artifact, so the cycle fails closed (#79)
    # rather than re-parking the gate over an unrevised document.
    assert status == M.RUN_PARKED
    cyc = man.record("cycle")
    assert cyc.status == M.PARKED
    assert cyc.parked_reason == M.PARKED_REASON_RESPONSE
    assert "vacuous convergence" in (cyc.notes or "")
    assert cyc.vacuous_parks == 1
    # The note is injected into the cycle as an audited decision that stamped
    # the artifact fingerprint the guard compared against.
    assert len(cyc.human_responses) == 1
    assert cyc.human_responses[0].response_text == "tighten the threat-model section"
    assert cyc.human_responses[0].user == "op@example.com"
    assert cyc.human_responses[0].artifact_fingerprint


def test_reject_gate_without_upstream_cycle_is_terminal(cycle_repo):
    """A gate with no upstream adversarial_cycle still fails terminally (no
    iterate path) — reject is never a silent no-op."""
    pipeline = Pipeline.model_validate({
        "name": "demo", "version": 1,
        "stages": [{"id": "s", "steps": [{"id": "gate", "type": "human_gate"}]}],
    })
    cfg = RunConfig.model_validate(BASE_CONFIG)
    man = Manifest(run_id="r", slug="demo", branch="b", base_branch="main",
                   pipeline=PipelineRef(name="demo", version=1, hash="h"))
    orch = Orchestrator(
        repo_root=cycle_repo, run_dir=cycle_repo / "runs" / "demo" / "run-1",
        artifact_root=cycle_repo, config=cfg, pipeline=pipeline, manifest=man,
        adapter_factory=lambda n: {}[n],
    )
    assert orch.drive() == M.RUN_PARKED
    assert orch.reject_gate("gate", notes="no") == M.RUN_FAILED
    assert man.record("gate").status == M.FAILED
    assert "no upstream adversarial_cycle" in man.record("gate").notes


# --- 1. parked_reason discriminator on cycle parks ------------------------------
def test_upstream_invalidation_park_sets_cycle_escalation_reason(cycle_repo):
    """An FR-10.4 upstream-invalidation park is response-resolvable now."""
    adapters = {
        "reviewer": SeqAdapter(REVIEW(F("F-001"))),
        # a non-rejected verdict whose fix lands in a different artifact → FR-10.4
        "triage": SeqAdapter(V("F-001", action="defer", target_artifact="plan.md")),
        "builder": SeqAdapter(),
    }
    status, man = _drive_with_record(
        cycle_repo, adapters, StepRecord(id="cycle", type="adversarial_cycle")
    )
    assert status == M.RUN_PARKED
    rec = man.record("cycle")
    assert rec.status == M.PARKED
    assert rec.parked_reason == M.PARKED_REASON_RESPONSE
    assert rec.parked_reason in M.RESPONSE_RESOLVABLE_PARK_REASONS
    assert "FR-10.4" in rec.notes


def test_max_rounds_escalation_sets_cycle_escalation_reason(cycle_repo):
    """An FR-10.5 max_rounds exhaustion with an open blocker is response-resolvable."""
    blocker = F("F-001", severity="blocking")
    adapters = {
        # blocking finding stays unresolved across both rounds → escalation
        "reviewer": SeqAdapter(
            REVIEW(blocker), CONFIRM(CV("F-001", "unresolved")),
            REVIEW(blocker), CONFIRM(CV("F-001", "unresolved")),
        ),
        "triage": SeqAdapter(V("F-001"), V("F-001")),
        "esc": SeqAdapter(V("F-001"), V("F-001")),  # escalation upholds fix_now
        "builder": SeqAdapter(
            writer("src.py", "a\n", {"done": True}),
            writer("src.py", "b\n", {"done": True}),
        ),
    }
    status, man = _drive_with_record(
        cycle_repo, adapters, StepRecord(id="cycle", type="adversarial_cycle"),
        step_extra={"escalation_agent": "esc"},
    )
    assert status == M.RUN_PARKED
    rec = man.record("cycle")
    assert rec.parked_reason == M.PARKED_REASON_RESPONSE
    assert "FR-10.5" in rec.notes


# --- 2. decision injection into the cycle's agents ------------------------------
SENTINEL = "OPERATOR-DECISION-SENTINEL: F-001 is in scope; stop flagging it"


def test_decision_injected_into_reviewer_and_triager(cycle_repo):
    """A recorded `--response` reaches the reviewer AND triager on re-drive,
    unwrapped (trusted), so they re-evaluate per the operator's ruling."""
    reviewer = SeqAdapter(REVIEW(F("F-001")), CONFIRM(CV("F-001")))
    triage = SeqAdapter(V("F-001"))
    builder = SeqAdapter(writer("src.py", "fixed\n", {"done": True}))
    rec = StepRecord(
        id="cycle", type="adversarial_cycle",
        human_responses=[_response(SENTINEL)],
    )
    status, man = _drive_with_record(
        cycle_repo, {"reviewer": reviewer, "triage": triage, "builder": builder},
        rec,
    )
    assert status == M.RUN_DONE
    review_prompt = reviewer.calls[0]["prompt"]
    triage_prompt = triage.calls[0]["prompt"]
    assert SENTINEL in review_prompt
    assert SENTINEL in triage_prompt
    assert "AUTHORITATIVE HUMAN DECISION" in review_prompt
    # the decision is a trusted instruction, NOT wrapped as untrusted reviewer data
    assert wrap_as_data(SENTINEL) not in review_prompt


def test_no_decision_block_without_responses(cycle_repo):
    """A cycle with no recorded responses injects nothing (existing behavior)."""
    reviewer = SeqAdapter(REVIEW(F("F-001")), CONFIRM(CV("F-001")))
    triage = SeqAdapter(V("F-001"))
    builder = SeqAdapter(writer("src.py", "fixed\n", {"done": True}))
    status, _ = _drive_with_record(
        cycle_repo, {"reviewer": reviewer, "triage": triage, "builder": builder},
        StepRecord(id="cycle", type="adversarial_cycle"),
    )
    assert status == M.RUN_DONE
    assert "AUTHORITATIVE HUMAN DECISION" not in reviewer.calls[0]["prompt"]


def test_human_decision_block_helper():
    """`_human_decision_block` is empty without responses; labelled with them."""
    empty = SimpleNamespace(record=SimpleNamespace(human_responses=[]))
    assert _human_decision_block(empty) == ""
    seeded = SimpleNamespace(record=SimpleNamespace(human_responses=[_response("X")]))
    block = _human_decision_block(seeded)
    assert "AUTHORITATIVE HUMAN DECISION" in block and "X" in block
    # #79: the preamble must mandate settled-vs-change-request classification —
    # the settling-only wording let a reviewer file a whole gate-rejection
    # change list as "already supplied decisions" and vacuously converge.
    assert "NOT yet reflected" in block
    assert "MUST raise it as a finding" in block
    assert "NEVER grounds for an empty findings array" in block
    assert "Gate-rejection notes are change requests" in block


def test_vacuous_convergence_guard(tmp_path):
    """#79 engine guard, all four legs: unchanged artifact parks first, the
    operator's re-affirmation proceeds with a recorded warning, a revised
    artifact skips the guard, and a fingerprint-less response skips it."""
    import hashlib

    from gauntlet.engine.cycle import _vacuous_convergence_result

    art = tmp_path / "prd.md"
    art.write_text("ARTIFACT BODY\n")
    fp = hashlib.sha256(art.read_bytes()).hexdigest()
    step = {"mode": "artifact", "artifact": "prd.md"}

    def _ctx(record):
        return SimpleNamespace(
            record=record, artifacts={"prd.md": art}, artifact_root=tmp_path
        )

    resp = _response("fix items 1-7")
    resp = resp.model_copy(update={"artifact_fingerprint": fp})
    rec = StepRecord(id="cycle", type="adversarial_cycle", human_responses=[resp])

    # 1. unchanged artifact + zero findings → fail-closed response park
    first = _vacuous_convergence_result(_ctx(rec), step, 1)
    assert first is not None and first.status == "parked"
    assert first.parked_reason == M.PARKED_REASON_RESPONSE
    assert "vacuous convergence" in first.notes
    assert rec.vacuous_parks == 1

    # 2. re-affirmed (vacuous_parks >= 1) → converge with a recorded warning
    second = _vacuous_convergence_result(_ctx(rec), step, 1)
    assert second is not None and second.status == "done"
    assert "WARNING" in second.notes and "unchanged" in second.notes

    # 3. artifact revised since the response → guard stands down
    art.write_text("REVISED BODY\n")
    fresh = StepRecord(id="cycle", type="adversarial_cycle", human_responses=[resp])
    assert _vacuous_convergence_result(_ctx(fresh), step, 1) is None

    # 4. no fingerprint (pre-#79 response) → guard stands down
    old = StepRecord(
        id="cycle", type="adversarial_cycle", human_responses=[_response("X")]
    )
    assert _vacuous_convergence_result(_ctx(old), step, 1) is None
    # 5. code_review mode has no governed artifact → guard stands down
    assert _vacuous_convergence_result(
        _ctx(rec), {"mode": "code_review"}, 1
    ) is None


# --- 3. RunManager._plan_response_action accepts a parked cycle -----------------
def _runmanager_repo(tmp_path) -> RunManager:
    repo = tmp_path / "repo"
    (repo / ".gauntlet").mkdir(parents=True)
    (repo / ".gauntlet" / "config.yaml").write_text(
        "base_branch: main\nrun_root: runs\nagents:\n  builder: {adapter: claude-code}\n"
    )
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email",
                    "op@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Op"],
                   check=True)
    return RunManager(repo)


def _parked_cycle_manifest() -> Manifest:
    man = Manifest(run_id="r", slug="demo", branch="b", base_branch="main",
                   status=M.RUN_PARKED,
                   pipeline=PipelineRef(name="demo", version=1, hash="h"))
    man.steps.append(StepRecord(
        id="cycle", type="adversarial_cycle", status=M.PARKED,
        parked_reason=M.PARKED_REASON_CYCLE_ESCALATION,
    ))
    return man


def test_plan_response_accepts_parked_cycle(tmp_path):
    mgr = _runmanager_repo(tmp_path)
    action = mgr._plan_response_action(_parked_cycle_manifest(), "ruling")
    assert action.kind == "append"
    assert action.step_id == "cycle"
    assert action.text == "ruling"
    assert action.user == "op@example.com"


def test_cycle_escalation_park_requires_response(tmp_path):
    mgr = _runmanager_repo(tmp_path)
    with pytest.raises(ValueError) as exc:
        mgr._plan_response_action(_parked_cycle_manifest(), None)
    msg = str(exc.value)
    assert "cycle escalation" in msg
    assert '--response' in msg


def test_plan_response_rejects_unrespondable_step(tmp_path):
    mgr = _runmanager_repo(tmp_path)
    man = _parked_cycle_manifest()
    man.steps[0].type = "shell"  # not a respondable step type
    with pytest.raises(ValueError) as exc:
        mgr._plan_response_action(man, "ruling")
    assert "only applies to" in str(exc.value)
    assert "adversarial_cycle" in str(exc.value)


def _failed_cycle_manifest() -> Manifest:
    man = Manifest(run_id="r", slug="demo", branch="b", base_branch="main",
                   status=M.RUN_FAILED,
                   pipeline=PipelineRef(name="demo", version=1, hash="h"))
    man.steps.append(StepRecord(
        id="cycle", type="adversarial_cycle", status=M.FAILED, attempts=1,
    ))
    return man


def test_plan_response_accepts_failed_cycle(tmp_path):
    """A failed cycle (e.g. fixer made no changes) is a 'blocked cycle' too —
    --response targets it so the human decision rides the re-drive."""
    mgr = _runmanager_repo(tmp_path)
    action = mgr._plan_response_action(_failed_cycle_manifest(), "decline F-001")
    assert action.kind == "append"
    assert action.step_id == "cycle"
    assert action.text == "decline F-001"


def test_plan_response_done_run_rejected(tmp_path):
    """A finished (non-parked, non-failed) run still rejects --response."""
    mgr = _runmanager_repo(tmp_path)
    man = _failed_cycle_manifest()
    man.status = M.RUN_DONE
    man.steps[0].status = M.DONE
    with pytest.raises(ValueError) as exc:
        mgr._plan_response_action(man, "ruling")
    assert "neither parked nor failed" in str(exc.value)


# --- 4. full end-to-end: gauntlet resume --response un-sticks a parked cycle ----
CYCLE_PIPELINE = """
name: cyc
version: 1
stages:
  - id: phase
    steps:
      - {id: cycle, type: adversarial_cycle, mode: artifact, artifact: prd.md,
         phase: P1, reviewer: reviewer, triager: triage, fixer: builder,
         max_rounds: 2}
"""

CYCLE_CONFIG = """
base_branch: main
run_root: runs
agents:
  reviewer: {adapter: codex}
  triage: {adapter: api, model: h}
  builder: {adapter: claude-code}
"""


def _build_cycle_runmanager(tmp_path, *, pipeline=CYCLE_PIPELINE, config=CYCLE_CONFIG):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Op"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email",
                    "op@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "commit.gpgsign", "false"],
                   check=True)
    (repo / "README.md").write_text("cycle resume fixture\n")
    (repo / ".gauntlet").mkdir()
    (repo / ".gauntlet" / "config.yaml").write_text(config)
    (repo / "pipelines").mkdir()
    (repo / "pipelines" / "cyc.yaml").write_text(pipeline)
    shutil.copytree(REPO / "schemas", repo / "schemas")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    subprocess.run(["git", "-C", str(repo), "branch", "-M", "main"], check=True)
    mgr = RunManager(repo)
    mgr.new("demo")
    mgr.layout("demo").prd_path.write_text("# PRD\n\nReal human-authored PRD body.\n")
    return repo, mgr


def test_resume_response_unsticks_parked_cycle_end_to_end(tmp_path):
    """The whole path: a cycle parks on an FR-10.4 upstream invalidation, then
    `gauntlet resume --response` records the decision, injects it on re-drive, and
    the cycle converges — the deadlock this feature fixes."""
    # Start: reviewer finds F-001; triage defers it to an upstream artifact → park.
    start = {
        "reviewer": SeqAdapter(REVIEW(F("F-001"))),
        "triage": SeqAdapter(V("F-001", action="defer", target_artifact="plan.md")),
        "builder": SeqAdapter(),
    }
    repo, mgr = _build_cycle_runmanager(tmp_path)
    status = mgr.start(
        "demo", repo / "pipelines" / "cyc.yaml", use_judge=False,
        adapter_factory=lambda n: start[n],
    )
    assert status == M.RUN_PARKED
    rec = mgr.status("demo").record("cycle")
    assert rec.parked_reason == M.PARKED_REASON_RESPONSE

    # Resume with a decision: reviewer still raises F-001, but triage now rejects
    # it (operator ruled it in scope) → the cycle accepts nothing → converges.
    resume_reviewer = SeqAdapter(REVIEW(F("F-001")))
    resume = {
        "reviewer": resume_reviewer,
        "triage": SeqAdapter(V("F-001", verdict="not_applicable", action="reject")),
        "builder": SeqAdapter(),
    }
    decision = "F-001 (the --from-repo report) is in scope for P1; do not defer it."
    status = mgr.resume(
        "demo", response=decision, use_judge=False,
        adapter_factory=lambda n: resume[n],
    )
    assert status == M.RUN_DONE

    rec = mgr.status("demo").record("cycle")
    assert rec.status == M.DONE
    assert rec.parked_reason is None  # cleared on the non-escalation outcome
    assert len(rec.human_responses) == 1
    entry = rec.human_responses[0]
    assert entry.state == M.RESPONSE_CONSUMED
    assert entry.response_id == "cycle-resp-1"
    assert entry.response_text == decision
    assert entry.user == "op@example.com"
    assert rec.attempts == 0  # a resolved escalation is not a failure (FR-6)
    # the decision actually reached the re-driven reviewer
    assert decision in resume_reviewer.calls[0]["prompt"]


def test_resume_response_unsticks_failed_cycle_end_to_end(tmp_path):
    """The P2-failure shape: a cycle FAILS because the fixer made no changes,
    then `gauntlet resume --response` re-drives it with the decision injected and
    it converges. Mirrors the real prd-authoring-aids P2 impl-cycle failure."""
    repo, mgr = _build_cycle_runmanager(tmp_path)
    # Start: F-001 accepted, but the fixer writes nothing → "fixer made no
    # changes ... failing closed" → the cycle FAILS (not parks).
    start = {
        "reviewer": SeqAdapter(REVIEW(F("F-001"))),
        "triage": SeqAdapter(V("F-001", action="fix_now")),
        "builder": SeqAdapter({"done": True}),  # returns, but writes no file
    }
    status = mgr.start(
        "demo", repo / "pipelines" / "cyc.yaml", use_judge=False,
        adapter_factory=lambda n: start[n],
    )
    assert status == M.RUN_FAILED
    rec = mgr.status("demo").record("cycle")
    assert rec.status == M.FAILED and "fixer made no changes" in rec.notes

    # Resume with a decision: triage now rejects F-001 → nothing to fix → converge.
    resume = {
        "reviewer": SeqAdapter(REVIEW(F("F-001"))),
        "triage": SeqAdapter(V("F-001", verdict="not_applicable", action="reject")),
        "builder": SeqAdapter(),
    }
    status = mgr.resume(
        "demo", response="F-001 is a non-issue; decline it.",
        use_judge=False, adapter_factory=lambda n: resume[n],
    )
    assert status == M.RUN_DONE
    rec = mgr.status("demo").record("cycle")
    assert rec.status == M.DONE
    entry = rec.human_responses[-1]
    assert entry.state == M.RESPONSE_CONSUMED
    assert entry.response_text == "F-001 is a non-issue; decline it."


# --- 5. FR-6.3 / review F-001: disposition_agent routing on a parked cycle -------
# A cycle whose `disposition_agent: mechanic` routes the `--response` classify to a
# cheap profile BEFORE re-driving the expensive review→triage→fix→confirm cycle: a
# re-park/malformed disposition returns without invoking the roles; only a proceed
# re-drives them. Mirrors the agent_task two-phase resume (test_resume_disposition).
DISPOSITION_CYCLE_PIPELINE = """
name: cyc
version: 1
stages:
  - id: phase
    steps:
      - {id: cycle, type: adversarial_cycle, mode: artifact, artifact: prd.md,
         phase: P1, reviewer: reviewer, triager: triage, fixer: builder,
         disposition_agent: mechanic, max_rounds: 2}
"""

DISPOSITION_CYCLE_CONFIG = """
base_branch: main
run_root: runs
agents:
  reviewer: {adapter: codex}
  triage: {adapter: api, model: h}
  builder: {adapter: claude-code}
  mechanic: {adapter: api, model: gpt-5-mini}
"""


def _disposition(disposition: str, *, responses=("cycle-resp-1",), artifact=None) -> dict:
    """A schema-valid resume-disposition object the cheap `mechanic` returns:
    `conflict` is null for a proceed, an object for a re-park (F-002 shape)."""
    obj: dict = {
        "disposition": disposition,
        "responses_considered": list(responses),
        "action_summary": f"{disposition} classification",
        "conflict": None,
    }
    if disposition in ("amendment_required", "new_conflict"):
        obj["conflict"] = {
            "summary": "still blocked", "requested_input": "a clearer decision",
            "artifact": artifact,
        }
    return obj


def _drive_disposition_cycle_to_park(tmp_path):
    """Start a disposition-routed cycle; park it on an FR-10.4 upstream invalidation."""
    repo, mgr = _build_cycle_runmanager(
        tmp_path, pipeline=DISPOSITION_CYCLE_PIPELINE, config=DISPOSITION_CYCLE_CONFIG
    )
    start = {
        "reviewer": SeqAdapter(REVIEW(F("F-001"))),
        "triage": SeqAdapter(V("F-001", action="defer", target_artifact="plan.md")),
        "builder": SeqAdapter(),
        "mechanic": SeqAdapter(),  # never touched on the initial park
    }
    status = mgr.start(
        "demo", repo / "pipelines" / "cyc.yaml", use_judge=False,
        adapter_factory=lambda n: start[n],
    )
    assert status == M.RUN_PARKED
    assert mgr.status("demo").record("cycle").parked_reason == M.PARKED_REASON_RESPONSE
    return repo, mgr


def test_cycle_routed_repark_classifies_on_mechanic_without_roles(tmp_path):
    # A re-park classification (new_conflict) is drafted by the cheap mechanic; the
    # expensive reviewer/triager/fixer are NEVER invoked on the re-drive.
    repo, mgr = _drive_disposition_cycle_to_park(tmp_path)
    mechanic = SeqAdapter(_disposition("new_conflict"))
    resume_reviewer = SeqAdapter()  # exhausts (raises) if the roles are invoked
    resume = {
        "reviewer": resume_reviewer, "triage": SeqAdapter(),
        "builder": SeqAdapter(), "mechanic": mechanic,
    }
    status = mgr.resume(
        "demo", response="still ambiguous", use_judge=False,
        adapter_factory=lambda n: resume[n],
    )
    assert status == M.RUN_PARKED
    rec = mgr.status("demo").record("cycle")
    assert rec.status == M.PARKED
    assert rec.parked_reason == M.PARKED_REASON_RESPONSE
    assert rec.human_responses[-1].state == M.RESPONSE_CONSUMED
    # the mechanic classified once; the roles were never invoked (window saved)
    assert len(mechanic.calls) == 1
    assert resume_reviewer.calls == []
    # the mechanic emission was bound to the resume-disposition schema
    assert mechanic.calls[0]["schema"]["$id"] == "gauntlet/schemas/resume-disposition.json"


def test_cycle_routed_proceed_classifies_then_redrives_roles(tmp_path):
    # A proceed classification by the mechanic re-drives the primary roles (only
    # they can do the review/triage/fix work), and the decision reaches the
    # reviewer on the re-drive.
    repo, mgr = _drive_disposition_cycle_to_park(tmp_path)
    mechanic = SeqAdapter(_disposition("proceed_in_place"))
    resume_reviewer = SeqAdapter(REVIEW(F("F-001")))
    resume = {
        "reviewer": resume_reviewer,
        "triage": SeqAdapter(V("F-001", verdict="not_applicable", action="reject")),
        "builder": SeqAdapter(),
        "mechanic": mechanic,
    }
    decision = "F-001 is in scope for P1; proceed, do not defer it."
    status = mgr.resume(
        "demo", response=decision, use_judge=False,
        adapter_factory=lambda n: resume[n],
    )
    assert status == M.RUN_DONE
    rec = mgr.status("demo").record("cycle")
    assert rec.status == M.DONE
    assert rec.parked_reason is None
    # mechanic classified first, THEN the roles were re-driven (reviewer ran)
    assert len(mechanic.calls) == 1
    assert resume_reviewer.calls, "a proceed must re-drive the primary roles"
    assert decision in resume_reviewer.calls[0]["prompt"]


def test_cycle_routed_malformed_disposition_fails_without_roles(tmp_path):
    # A mechanic that emits no valid disposition fails the step closed — the
    # expensive roles are never invoked on an unparseable classification.
    repo, mgr = _drive_disposition_cycle_to_park(tmp_path)
    mechanic = SeqAdapter({"unexpected": "shape"})  # no `disposition` field
    resume_reviewer = SeqAdapter()  # exhausts (raises) if invoked
    resume = {
        "reviewer": resume_reviewer, "triage": SeqAdapter(),
        "builder": SeqAdapter(), "mechanic": mechanic,
    }
    status = mgr.resume(
        "demo", response="proceed", use_judge=False,
        adapter_factory=lambda n: resume[n],
    )
    assert status == M.RUN_FAILED
    assert len(mechanic.calls) == 1
    assert resume_reviewer.calls == []  # builder/reviewer window untouched


def test_cycle_unset_disposition_agent_uses_roles_directly(tmp_path):
    # The negative control: with no `disposition_agent`, a `--response` resume
    # re-drives the roles directly (today's behavior) — no classify gate, no
    # mechanic. Uses the plain CYCLE_PIPELINE (no disposition_agent).
    repo, mgr = _build_cycle_runmanager(tmp_path)
    start = {
        "reviewer": SeqAdapter(REVIEW(F("F-001"))),
        "triage": SeqAdapter(V("F-001", action="defer", target_artifact="plan.md")),
        "builder": SeqAdapter(),
    }
    assert mgr.start(
        "demo", repo / "pipelines" / "cyc.yaml", use_judge=False,
        adapter_factory=lambda n: start[n],
    ) == M.RUN_PARKED
    resume_reviewer = SeqAdapter(REVIEW(F("F-001")))
    resume = {
        "reviewer": resume_reviewer,
        "triage": SeqAdapter(V("F-001", verdict="not_applicable", action="reject")),
        "builder": SeqAdapter(),
    }
    status = mgr.resume(
        "demo", response="F-001 in scope; proceed", use_judge=False,
        adapter_factory=lambda n: resume[n],
    )
    assert status == M.RUN_DONE
    # the reviewer ran directly on the re-drive — no gate short-circuited it
    assert resume_reviewer.calls

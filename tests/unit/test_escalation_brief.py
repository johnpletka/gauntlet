"""Escalation parks carry a pre-digested fix contract, not raw findings (#163).

Covers the deterministic digest (claim, residual gap, fix history) written at
every cycle escalation park; the optional ``fix_contract_agent`` contract
(attached by id, fail-soft on every failure, never changing the park); the
re-drive injection that hands the answered contracts to the reviewer and the
fixer; and the ``status`` / ``status --json`` projection.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from gauntlet.adapters.base import (
    FAILURE_TRANSIENT_USAGE_LIMIT,
    AgentFailedError,
    FailureInfo,
)
from gauntlet.adapters._structured import validate_schema
from gauntlet.engine import escalation_brief as EB
from gauntlet.engine import manifest as M
from gauntlet.engine import operator as op
from gauntlet.engine.cycle import DATA_BEGIN, _fix_prompt
from gauntlet.engine.manifest import StepRecord
from gauntlet.web.intel import escalation_sub_kinds, extract_finding_ids

from test_cycle import (
    BASE_CONFIG,
    CONFIRM,
    CV,
    F,
    REVIEW,
    SeqAdapter,
    V,
    cycle_repo,  # noqa: F401  (pytest fixture)
    run_cycle,
    writer,
)
from test_cycle_resume_response import _drive_with_record, _response

REPO = Path(__file__).resolve().parents[2]
CONTRACT_CONFIG = {
    **BASE_CONFIG,
    "agents": {**BASE_CONFIG["agents"], "contract": {"adapter": "api", "model": "s"}},
}


def _brief_path(run_dir: Path) -> Path:
    return run_dir / "artifacts" / EB.BRIEF_ARTIFACT


def _exhausting_adapters(**extra):
    """A blocking finding that stays unresolved across both rounds → FR-10.5."""
    reviewer = SeqAdapter(
        REVIEW(F("F-001", "blocking", claim="lock order inverted")),
        {"verdicts": [{"finding_id": "F-001", "verdict": "partially_resolved",
                       "notes": "round 1 moved the lock but the reader still races"}],
         "new_findings": [], "summary": ""},
        REVIEW(F("F-001", "blocking", claim="lock order inverted")),
        {"verdicts": [{"finding_id": "F-001", "verdict": "unresolved",
                       "notes": "advisory lock still taken after the write"}],
         "new_findings": [], "summary": ""},
    )
    adapters = {
        "reviewer": reviewer,
        "triage": SeqAdapter(V("F-001"), V("F-001")),
        "esc": SeqAdapter(V("F-001"), V("F-001")),
        "builder": SeqAdapter(writer("src.py", "try 1\n", {}),
                              writer("src.py", "try 2\n", {})),
    }
    adapters.update(extra)
    return adapters


def _contract(fid="F-001", rec="fix_in_scope"):
    return {"finding_id": fid, "proposed_change": "take the advisory lock before the write",
            "grounding": "PRD §4 locking contract", "acceptance_check":
            "a test interleaves reader and writer and sees no torn read",
            "recommendation": rec}


# --- the deterministic digest -----------------------------------------------
def test_max_rounds_park_writes_digest_without_contract_agent(cycle_repo):
    status, man, run_dir = run_cycle(
        cycle_repo, _exhausting_adapters(), step_extra={"escalation_agent": "esc"}
    )
    assert status == M.RUN_PARKED
    rec = man.record("cycle")
    assert rec.parked_reason == M.PARKED_REASON_RESPONSE
    brief = json.loads(_brief_path(run_dir).read_text())
    assert brief["kind"] == EB.KIND_MAX_ROUNDS and brief["step_id"] == "cycle"
    assert brief["contract"]["status"] == EB.CONTRACT_NOT_CONFIGURED
    [item] = brief["findings"]
    assert item["finding_id"] == "F-001" and item["claim"] == "lock order inverted"
    # Residual gap = the latest confirm; fix history = both rounds, with SHAs.
    assert item["residual_gap"] == {
        "verdict": "unresolved", "notes": "advisory lock still taken after the write"}
    fix_shas = [c.sha for c in man.commits if c.phase.startswith("P5.")]
    assert [e["fix_sha"] for e in item["fix_rounds"]] == fix_shas
    assert [e["verdict"] for e in item["fix_rounds"]] == ["partially_resolved", "unresolved"]
    assert "reader still races" in item["fix_rounds"][0]["notes"]
    assert item["proposed_contract"] is None
    assert "approve" in item["decision_owed"] and "reject" in item["decision_owed"]
    # The park note points at the brief without disturbing note classification.
    assert EB.BRIEF_ARTIFACT in rec.notes
    assert escalation_sub_kinds(rec.notes) == ["open_blocker"]
    assert extract_finding_ids(rec.notes) == ["F-001"]


def test_closure_guard_park_writes_brief(cycle_repo):
    adapters = {
        "reviewer": SeqAdapter(REVIEW(F("F-001"))),
        "triage": SeqAdapter(V("F-001", action="defer", target_artifact="plan.md")),
        "builder": SeqAdapter(),
    }
    status, man, run_dir = run_cycle(cycle_repo, adapters)
    assert status == M.RUN_PARKED
    brief = json.loads(_brief_path(run_dir).read_text())
    assert brief["kind"] == EB.KIND_CLOSURE
    [item] = brief["findings"]
    assert item["triage"]["target_artifact"] == "plan.md"
    assert item["fix_rounds"] == [] and item["residual_gap"] is None
    assert "plan.md" in item["decision_owed"]


def test_triage_park_without_escalation_agent_writes_brief(cycle_repo):
    adapters = {
        "reviewer": SeqAdapter(REVIEW(F("F-001", "blocking"), F("F-002", "minor"))),
        "triage": SeqAdapter(V("F-001", "not_applicable", "reject"), V("F-002")),
        "builder": SeqAdapter(),
    }
    status, man, run_dir = run_cycle(cycle_repo, adapters)
    assert status == M.RUN_PARKED
    brief = json.loads(_brief_path(run_dir).read_text())
    assert brief["kind"] == EB.KIND_TRIAGE
    # Only the finding that needs a human, not the routine minor one.
    assert [it["finding_id"] for it in brief["findings"]] == ["F-001"]


def test_convergence_drops_this_steps_brief(cycle_repo):
    run_dir = cycle_repo / "runs" / "demo" / "run-1"
    (run_dir / "artifacts").mkdir(parents=True)
    _brief_path(run_dir).write_text(json.dumps(
        {"step_id": "cycle", "iteration": None, "response_count": 0, "findings": []}))
    adapters = {
        "reviewer": SeqAdapter(REVIEW(F("F-001")), CONFIRM(CV("F-001"))),
        "triage": SeqAdapter(V("F-001")),
        "builder": SeqAdapter(writer("src.py", "fixed\n", {})),
    }
    status, _, _ = run_cycle(cycle_repo, adapters)
    assert status == M.RUN_DONE
    assert not _brief_path(run_dir).exists()  # absent > stale


def test_convergence_keeps_another_steps_brief(cycle_repo):
    run_dir = cycle_repo / "runs" / "demo" / "run-1"
    (run_dir / "artifacts").mkdir(parents=True)
    _brief_path(run_dir).write_text(json.dumps(
        {"step_id": "other-cycle", "iteration": None, "findings": []}))
    adapters = {"reviewer": SeqAdapter(REVIEW()), "triage": SeqAdapter(),
                "builder": SeqAdapter()}
    status, _, _ = run_cycle(cycle_repo, adapters)
    assert status == M.RUN_DONE
    assert _brief_path(run_dir).exists()


# --- the proposed contract (fix_contract_agent) -------------------------------
def test_contract_agent_attaches_contract_by_id(cycle_repo):
    contract = SeqAdapter({"contracts": [_contract(), _contract("F-999")],
                           "summary": "one in-scope fix"})
    status, man, run_dir = run_cycle(
        cycle_repo, _exhausting_adapters(contract=contract), config=CONTRACT_CONFIG,
        step_extra={"escalation_agent": "esc", "fix_contract_agent": "contract"},
    )
    assert status == M.RUN_PARKED
    assert man.record("cycle").parked_reason == M.PARKED_REASON_RESPONSE
    brief = json.loads(_brief_path(run_dir).read_text())
    assert brief["contract"]["status"] == EB.CONTRACT_PROPOSED
    assert brief["contract"]["dropped_ids"] == ["F-999"]  # never shown as applying
    pc = brief["findings"][0]["proposed_contract"]
    assert pc["proposed_change"] == "take the advisory lock before the write"
    assert pc["recommendation"] == "fix_in_scope"
    # One call, with the fix-contract schema, the approved artifact inlined, and
    # the digest (incl. the fix history) wrapped as untrusted data.
    [call] = contract.calls
    assert call["schema"] == json.loads((REPO / "schemas" / "fix-contract.json").read_text())
    assert "ARTIFACT-BODY-SENTINEL" in call["prompt"]
    assert DATA_BEGIN in call["prompt"] and "reader still races" in call["prompt"]
    assert call["prompt"].index(DATA_BEGIN) < call["prompt"].index("reader still races")


def test_contract_agent_failure_degrades_to_digest(cycle_repo):
    contract = SeqAdapter(ValueError("adapter blew up"))
    status, man, run_dir = run_cycle(
        cycle_repo, _exhausting_adapters(contract=contract), config=CONTRACT_CONFIG,
        step_extra={"escalation_agent": "esc", "fix_contract_agent": "contract"},
    )
    assert status == M.RUN_PARKED  # the park stands
    assert man.record("cycle").parked_reason == M.PARKED_REASON_RESPONSE
    brief = json.loads(_brief_path(run_dir).read_text())
    assert brief["contract"]["status"] == EB.CONTRACT_UNAVAILABLE
    assert "adapter blew up" in brief["contract"]["error"]
    assert brief["findings"][0]["residual_gap"]["verdict"] == "unresolved"
    assert "contract drafting failed" in man.record("cycle").notes


def test_contract_agent_usage_limit_does_not_become_a_quota_park(cycle_repo):
    # An advisory call hitting the quota must not turn a human-decision park
    # into a usage_limit park that a plain resume would "fix".
    info = FailureInfo(kind=FAILURE_TRANSIENT_USAGE_LIMIT, marker="usage limit",
                       retry_after_s=None)
    contract = SeqAdapter(AgentFailedError("limit", failure_info=info))
    status, man, run_dir = run_cycle(
        cycle_repo, _exhausting_adapters(contract=contract), config=CONTRACT_CONFIG,
        step_extra={"escalation_agent": "esc", "fix_contract_agent": "contract"},
    )
    assert status == M.RUN_PARKED
    assert man.record("cycle").parked_reason == M.PARKED_REASON_RESPONSE
    brief = json.loads(_brief_path(run_dir).read_text())
    assert brief["contract"]["status"] == EB.CONTRACT_UNAVAILABLE
    assert "usage_limit" in brief["contract"]["error"]


def test_contract_agent_that_dirties_the_tree_is_discarded(cycle_repo):
    contract = SeqAdapter(writer("stray.txt", "oops\n",
                                 {"contracts": [_contract()], "summary": ""}))
    status, man, run_dir = run_cycle(
        cycle_repo, _exhausting_adapters(contract=contract), config=CONTRACT_CONFIG,
        step_extra={"escalation_agent": "esc", "fix_contract_agent": "contract"},
    )
    assert status == M.RUN_PARKED
    brief = json.loads(_brief_path(run_dir).read_text())
    assert brief["contract"]["status"] == EB.CONTRACT_UNAVAILABLE
    assert "modified the worktree" in brief["contract"]["error"]
    assert brief["findings"][0]["proposed_contract"] is None
    assert (cycle_repo / "stray.txt").exists()  # reported, never auto-reverted


def test_merge_contracts_with_no_match_is_unavailable():
    brief = EB.build_brief(
        step_id="c", iteration=None, phase="P1", kind=EB.KIND_CLOSURE, notes="n",
        open_findings=[F("F-001")], verdicts_by_id={}, history=EB.FixHistory(),
        response_count=0,
    )
    EB.merge_contracts(brief, "contract", {"contracts": [_contract("F-404")],
                                           "summary": ""})
    assert brief["contract"]["status"] == EB.CONTRACT_UNAVAILABLE
    assert brief["contract"]["dropped_ids"] == ["F-404"]


def test_builtin_contract_schema_matches_shipped_file():
    shipped = json.loads((REPO / "schemas" / "fix-contract.json").read_text())
    builtin = EB.BUILTIN_CONTRACT_SCHEMA

    def shape(s):
        item = s["properties"]["contracts"]["items"]
        return (s["required"], sorted(s["properties"]), item["required"],
                sorted(item["properties"]),
                item["properties"]["recommendation"]["enum"])

    assert shape(shipped) == shape(builtin)
    scaffold = REPO / "src" / "gauntlet" / "scaffold" / "schemas" / "fix-contract.json"
    assert scaffold.read_text() == (REPO / "schemas" / "fix-contract.json").read_text()


def test_fix_history_carries_parent_rounds_into_remainder():
    h = EB.FixHistory()
    h.record_round(1, "aaa", ["F-001"], {"verdicts": [
        {"finding_id": "F-001", "verdict": "partially_resolved", "notes": "half"}]},
        [{"id": "F-001-r1-c0", "carried_from": "F-001"}])
    h.record_round(2, "bbb", ["F-001-r1-c0"], {"verdicts": []})
    hist = h.history_for("F-001-r1-c0")
    assert [(e["round"], e["fix_sha"], e["verdict"]) for e in hist] == [
        (1, "aaa", "partially_resolved"), (2, "bbb", "unresolved")]
    assert "no confirm verdict" in hist[1]["notes"]  # missing verdict fails closed


# --- the re-drive that answers the brief -----------------------------------
def _seed_brief(repo: Path, *, response_count: int) -> None:
    run_dir = repo / "runs" / "demo" / "run-1"
    (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    brief = EB.build_brief(
        step_id="cycle", iteration=None, phase="P5", kind=EB.KIND_MAX_ROUNDS,
        notes="escalation (FR-10.5): ...", open_findings=[F("F-001", "blocking")],
        verdicts_by_id={}, history=EB.FixHistory(), response_count=response_count,
    )
    EB.merge_contracts(brief, "contract", {"contracts": [{
        **_contract(), "proposed_change": "CONTRACT-SENTINEL: lock before write"}],
        "summary": ""})
    _brief_path(run_dir).write_text(json.dumps(brief))


def test_response_redrive_hands_answered_contract_to_reviewer_and_fixer(cycle_repo):
    _seed_brief(cycle_repo, response_count=0)
    reviewer = SeqAdapter(REVIEW(F("F-001")), CONFIRM(CV("F-001")))
    builder = SeqAdapter(writer("src.py", "fixed\n", {}))
    adapters = {"reviewer": reviewer, "triage": SeqAdapter(V("F-001")),
                "builder": builder}
    record = StepRecord(id="cycle", type="adversarial_cycle",
                        human_responses=[_response("F-001: approve as proposed")])
    status, _ = _drive_with_record(cycle_repo, adapters, record)
    assert status == M.RUN_DONE
    review_prompt = reviewer.calls[0]["prompt"]
    fix_prompt = builder.calls[0]["prompt"]
    for prompt in (review_prompt, fix_prompt):
        assert "CONTRACT-SENTINEL" in prompt
        assert "F-001: approve as proposed" in prompt
        # agent-drafted → data, after the trusted decision
        assert prompt.index("PROPOSED FIX CONTRACT") < prompt.rindex(DATA_BEGIN)
    # converged → the answered brief is gone (absent > stale)
    assert not _brief_path(cycle_repo / "runs" / "demo" / "run-1").exists()


def test_brief_newer_than_the_response_is_not_injected(cycle_repo):
    # response_count == len(responses): the brief was written AFTER the latest
    # response, i.e. a newer park the human has not answered yet.
    _seed_brief(cycle_repo, response_count=1)
    reviewer = SeqAdapter(REVIEW(F("F-001")), CONFIRM(CV("F-001")))
    adapters = {"reviewer": reviewer, "triage": SeqAdapter(V("F-001")),
                "builder": SeqAdapter(writer("src.py", "fixed\n", {}))}
    record = StepRecord(id="cycle", type="adversarial_cycle",
                        human_responses=[_response("carry on")])
    _drive_with_record(cycle_repo, adapters, record)
    assert "CONTRACT-SENTINEL" not in reviewer.calls[0]["prompt"]


def test_fix_prompt_without_response_is_unchanged(tmp_path):
    ctx = SimpleNamespace(
        run_dir=tmp_path, repo_root=tmp_path,
        config=SimpleNamespace(asset_root="."),
        record=SimpleNamespace(id="cycle", iteration=None, human_responses=[]),
    )
    prompt = _fix_prompt({}, ctx, {"F-001": F("F-001")},
                         [V("F-001")])
    assert "PROPOSED FIX CONTRACT" not in prompt
    assert "AUTHORITATIVE HUMAN DECISION" not in prompt


# --- status surfaces ---------------------------------------------------------
def test_status_escalation_block_is_schema_valid_and_redacted(cycle_repo, monkeypatch):
    contract = SeqAdapter({"contracts": [_contract()], "summary": "s"})
    _, man, run_dir = run_cycle(
        cycle_repo, _exhausting_adapters(contract=contract), config=CONTRACT_CONFIG,
        step_extra={"escalation_agent": "esc", "fix_contract_agent": "contract"},
    )
    rec = man.record("cycle")
    block = op.compute_escalation_context(run_dir, rec)
    validate_schema(block, op.STATUS_SCHEMA["properties"]["escalation"])
    assert block["contract_status"] == "proposed" and block["responses_since"] == 0
    assert block["findings"][0]["proposed_contract"]["recommendation"] == "fix_in_scope"

    from gauntlet.logging import redact as R

    class _Mask:
        def redact(self, text):
            return text.replace("advisory lock", "[MASKED]"), 1

    monkeypatch.setattr(R, "build_redactor", lambda settings=None: _Mask())
    masked = op.compute_escalation_context(run_dir, rec)
    assert "[MASKED]" in masked["findings"][0]["residual_gap"]["notes"]
    assert "advisory lock" not in json.dumps(masked)

    lines = EB.render_lines(block, slug="demo")
    text = "\n".join(lines)
    assert "proposed contract [fix_in_scope]" in text
    assert "residual gap (unresolved)" in text
    assert "gauntlet resume demo --response" in text


def test_status_escalation_none_for_other_steps(tmp_path):
    assert op.compute_escalation_context(
        tmp_path, StepRecord(id="g", type="human_gate")) is None
    assert op.compute_escalation_context(
        tmp_path, StepRecord(id="cycle", type="adversarial_cycle")) is None  # no brief

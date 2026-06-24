"""P6 — end-to-end synthetic gate: the assembled resume-with-response mechanism
un-sticks a parked run (PRD §8 e2e, §11; FR-7).

This is the **repeatable CI gate** the PRD §8 acceptance calls for: an isolated,
disposable run created fresh and torn down within the test (tmp_path), driven by
a deterministic scripted adapter — no live creds, no real run mutated, safe to
run before every handoff. Where the P3/P5 unit suites each pin one phase in
isolation, this exercises every prior phase *together* in one cycle:

- P1 conflict-park discriminator (`parked_reason`),
- P3 idempotent pending→consumed recording + orchestrator-owned checkpoint
  commits + retry-budget decoupling,
- P4 chronological `human-response.md` prompt injection,
- P5 structured-disposition → step-outcome mapping,

and asserts the run transitions out of `parked`, the manifest audit trail is
complete (ids / state / user / timestamp, `attempts` vs `len(human_responses)`),
and a commit referencing the consumed `response_id` reaches git history.

The one-time dogfood of the real `prd-authoring-aids` run is a human-gated
runbook (`runs/prd-authoring-aids/DOGFOOD-RUNBOOK.md`), **not** a pytest — it
mutates one real parked run and depends on live creds, so it cannot satisfy its
preconditions twice (PRD §8). It is deliberately absent here.
"""

from __future__ import annotations

from pathlib import Path

from gauntlet.engine import gitops, manifest as M

from test_resume_response import (
    PIPELINE,
    ScriptedAdapter,
    _build_repo,
    _checkpoint_log,
    _clock,
    _drive_to_conflict,
)


def test_e2e_conflict_resume_proceeds_unsticks_run(tmp_path):
    # The canonical end-to-end gate: a fresh, disposable run parks on an UPSTREAM
    # CONFLICT, a single `--response` resolves it, and the run drives to DONE with
    # a complete, auditable trail — every prior phase assembled into one cycle.
    repo, mgr = _build_repo(tmp_path / "repo")

    # P1: the run parks specifically on a conflict (discriminator set).
    _drive_to_conflict(repo, mgr)
    assert mgr.status("demo").status == M.RUN_PARKED

    # The human supplies a decision; the resumed builder proceeds in place.
    adapter = ScriptedAdapter("proceed")
    decision = "Ratify option 1: the conflict is resolved; no contradiction remains."
    status = mgr.resume(
        "demo", response=decision, use_judge=False,
        adapter_factory=lambda n: adapter, clock=_clock(),
    )

    # FR-7 / §11: the run is un-stuck — it left `parked` and completed.
    assert status == M.RUN_DONE
    man = mgr.status("demo")
    assert man.status == M.RUN_DONE

    rec = man.record("implement")
    assert rec.status == M.DONE
    # P1: a resolved conflict clears the discriminator (current-state, not a latch).
    assert rec.parked_reason is None

    # P3 / FR-2: exactly one fully-formed audit entry, terminal state consumed.
    assert len(rec.human_responses) == 1
    entry = rec.human_responses[0]
    assert entry.response_id == "implement-resp-1"
    assert entry.response_attempt == 1
    assert entry.state == M.RESPONSE_CONSUMED
    assert entry.response_text == decision
    assert entry.user == "fixture@gauntlet.local"  # FR-9 git-config fallback
    assert entry.timestamp.startswith("2026-06-24T00:00:")  # injected clock

    # FR-6: a conflict→proceed cycle is not a failure; the retry counter never moved
    # even though the response history grew.
    assert rec.attempts == 0
    assert rec.attempts < len(rec.human_responses)

    # P4: the builder actually received the decision via the synthetic artifact.
    resume_prompt = adapter.prompts[-1]
    assert "--- input artifact: human-response.md ---" in resume_prompt
    assert "## Response implement-resp-1 — attempt 1" in resume_prompt
    assert decision in resume_prompt

    # P5 / FR-10: the run's behaviour is driven by the builder's STRUCTURED
    # disposition, not just text — assert the exact fields the fixture emitted
    # (the deterministic oracle PRD §8/FR-10 require, not prompt substrings). The
    # proceed cycle emitted exactly one disposition, naming the consumed response.
    assert len(adapter.dispositions) == 1
    disp = adapter.dispositions[0]
    assert disp["disposition"] == "proceed_in_place"
    assert disp["responses_considered"] == ["implement-resp-1"]
    assert disp["conflict"] is None  # proceed carries no conflict body

    # P3 / FR-2.2: both response states reach git history under the engine
    # identity, pending before consumed, and the consumed checkpoint names the
    # consumed response_id (a commit referencing it, PRD §8 e2e).
    assert _checkpoint_log(repo) == [
        "Gauntlet Engine|gauntlet: response implement-resp-1 pending",
        "Gauntlet Engine|gauntlet: response implement-resp-1 consumed",
    ]
    # The phase commit itself landed (the proceed path committed real work).
    assert gitops.commit_subject(repo, "HEAD") == "P1: implement phase"
    assert (repo / "feature.py").exists()
    # PRD §8 / appendix: the implementation phase commit BODY references the
    # consumed response_id, linking the committed code to the human decision that
    # ratified it — the audit linkage the bookkeeping checkpoint alone cannot
    # stand in for (F-001).
    phase_body = gitops.commit_message(repo, "HEAD")
    assert "Gauntlet-Response: implement-resp-1" in phase_body


def test_e2e_multi_cycle_repark_then_resolve(tmp_path):
    # FR-7 / §11: the mechanism un-sticks a run even when the first decision does
    # not resolve it. A new_conflict re-parks (no forced loop), and a *second*
    # `--response` finally resolves it — with the audit history accumulating
    # append-only across both cycles and the failure budget untouched throughout.
    repo, mgr = _build_repo(tmp_path / "repo")
    _drive_to_conflict(repo, mgr)

    # First decision is still ambiguous → the builder re-parks (new_conflict).
    repark = ScriptedAdapter("conflict")
    status = mgr.resume(
        "demo", response="do the right thing", use_judge=False,
        adapter_factory=lambda n: repark, clock=_clock(),
    )
    assert status == M.RUN_PARKED
    rec = mgr.status("demo").record("implement")
    assert rec.parked_reason == M.PARKED_REASON_UPSTREAM_CONFLICT  # still a conflict
    assert rec.attempts == 0  # a re-park is not a failure (FR-6)
    assert len(rec.human_responses) == 1
    assert rec.human_responses[0].state == M.RESPONSE_CONSUMED

    # FR-10: the re-park is driven by a structured `new_conflict` disposition that
    # names the consumed response and carries a conflict body whose
    # `requested_input` states what the supplied decision still did not provide
    # (asserted on the structured fields directly, PRD §8, not prose).
    assert len(repark.dispositions) == 1
    repark_disp = repark.dispositions[0]
    assert repark_disp["disposition"] == "new_conflict"
    assert repark_disp["responses_considered"] == ["implement-resp-1"]
    assert repark_disp["conflict"]["requested_input"] == (
        "a decision that unambiguously resolves it"
    )

    # Second decision resolves it → the run completes.
    adapter = ScriptedAdapter("proceed")
    status = mgr.resume(
        "demo", response="Resolved: implement option 1 as specified.",
        use_judge=False, adapter_factory=lambda n: adapter, clock=_clock(),
    )
    assert status == M.RUN_DONE

    rec = mgr.status("demo").record("implement")
    assert rec.status == M.DONE
    assert rec.parked_reason is None  # cleared once resolved

    # Append-only audit history: two consumed responses, ordinals 1 and 2.
    assert [r.response_id for r in rec.human_responses] == [
        "implement-resp-1", "implement-resp-2"
    ]
    assert [r.response_attempt for r in rec.human_responses] == [1, 2]
    assert all(r.state == M.RESPONSE_CONSUMED for r in rec.human_responses)
    # Two conflict cycles, zero failures (FR-6).
    assert rec.attempts == 0

    # FR-10: the resolving cycle emitted a `proceed_in_place` disposition that
    # considered the FULL ordered response history (both ids), with no conflict
    # body — the structured oracle for "the run is genuinely unblocked".
    assert len(adapter.dispositions) == 1
    final_disp = adapter.dispositions[0]
    assert final_disp["disposition"] == "proceed_in_place"
    assert final_disp["responses_considered"] == [
        "implement-resp-1", "implement-resp-2"
    ]
    assert final_disp["conflict"] is None

    # P4: the second resume's prompt carries the FULL ordered history.
    final_prompt = adapter.prompts[-1]
    assert final_prompt.count("--- input artifact: human-response.md ---") == 1
    assert final_prompt.index("implement-resp-1") < final_prompt.index("implement-resp-2")

    # Both response cycles' checkpoints are in git history, in order.
    assert _checkpoint_log(repo) == [
        "Gauntlet Engine|gauntlet: response implement-resp-1 pending",
        "Gauntlet Engine|gauntlet: response implement-resp-1 consumed",
        "Gauntlet Engine|gauntlet: response implement-resp-2 pending",
        "Gauntlet Engine|gauntlet: response implement-resp-2 consumed",
    ]
    assert gitops.commit_subject(repo, "HEAD") == "P1: implement phase"
    # PRD §8 / appendix: the phase commit body references BOTH consumed responses
    # — the full audit linkage from the committed code to every human decision in
    # the cycle that produced it (F-001).
    phase_body = gitops.commit_message(repo, "HEAD")
    assert "Gauntlet-Response: implement-resp-1, implement-resp-2" in phase_body

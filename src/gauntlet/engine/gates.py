"""Evidence-tiered gate predicate (pipeline-effectiveness FR-4, P8).

A per-phase **code** gate configured ``policy: auto_when_clean`` auto-approves
only when the strict §4.2 clean-signal conjunction holds:

    converged in round 1 · zero blocking/major legitimate findings ·
    acceptance gate passed · tests green · zero escalations ·
    zero reviewer mutations · verifier ran clean

Any single ambiguous signal parks for a human exactly as ``policy: always``
would — fail closed (CLAUDE.md §2). The predicate is a **pure function over
facts the pipeline already records** (the manifest step records, their metrics,
and the round findings/triage artifacts, P1–P5): no new judgment call, no LLM.
Keeping it here — separate from the orchestrator's control flow — makes it
directly unit-testable against a synthetic manifest, which is how every P8-A1
single-violation-parks fixture drives it.

The orchestrator consumes :func:`evaluate_clean_gate` when a ``human_gate`` step
carries ``policy: auto_when_clean``; a clean decision becomes an ``auto_approval``
manifest record, a miss falls through to the normal human park.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from gauntlet.engine import manifest as M
from gauntlet.engine.pipeline import Pipeline, Stage, Step

# Severities that make a *legitimate* finding a predicate miss (§4.2). A minor/nit
# legitimate finding does not block auto-approval; a blocking/major one does.
_BLOCKING_SEVERITIES = frozenset({"blocking", "major"})

# The triage verdict marking a finding a real defect (schemas/triage.json). Only a
# ``legitimate`` verdict counts against the "zero blocking/major legitimate
# findings" conjunct — a bikeshedding/premature/not-applicable finding does not.
_LEGITIMATE_VERDICT = "legitimate"

# Substring identifying a synthetic reviewer-mutation finding (cycle.py stamps the
# id ``F-R<round>-MUTATION-<seq>`` when a reviewer mutates the worktree, FR-9.6).
# A reviewer mutation is a predicate miss regardless of its triage verdict — the
# read-only contract was violated, which a human must see.
_MUTATION_MARKER = "-MUTATION-"

# The type ``FindingsLoader`` returns: (findings list, triage-verdict list) read
# from this phase's round artifacts. Raising signals the artifacts are missing or
# unparseable — the caller fails that conjunct closed (cannot prove clean).
FindingsLoader = Callable[[], "tuple[list[dict[str, Any]], list[dict[str, Any]]]"]


def _phase_num(phase: str | None) -> int | None:
    """Parse a numeric phase prefix (``P8`` → 8), else ``None``."""
    if not phase:
        return None
    text = str(phase).split(".")[0]
    if text.upper().startswith("P") and text[1:].isdigit():
        return int(text[1:])
    return None


def record_reversals(
    manifest: M.Manifest,
    *,
    min_phase_num: int,
    user: str,
    notes: str,
    at: str,
) -> int:
    """Record human reversals of auto-approved gates ≥ ``min_phase_num`` (FR-4.2).

    Stamps every not-yet-reversed ``auto_approval`` whose numeric phase is at or
    beyond the rolled-back boundary, and — iff any matched — flips the run's
    effective auto-approval policy to ``always`` for the remainder of the run
    (:attr:`Manifest.auto_approval_disabled`). Returns the count reversed. This
    is the deterministic in-run circuit breaker: after a recorded reversal the
    clean predicate can no longer auto-approve, because a human signalled
    distrust (§9). Append-only — the reversed record is stamped, never deleted.
    """
    reversed_n = 0
    for rec in manifest.auto_approvals:
        if rec.reversed_at is not None:
            continue
        pnum = _phase_num(rec.phase)
        if pnum is not None and pnum >= min_phase_num:
            rec.reversed_at = at
            rec.reversed_by = user
            rec.reversal_notes = notes
            reversed_n += 1
    if reversed_n:
        manifest.auto_approval_disabled = True
    return reversed_n


@dataclass
class CleanGateDecision:
    """The outcome of the §4.2 clean-signal predicate for one code gate.

    ``clean`` is the strict conjunction; ``evidence`` is the full §6 snapshot
    stamped into the ``auto_approval`` record whether or not it passed (so a
    parked-for-a-miss gate's evidence is still recorded in the notes); ``misses``
    names every failed conjunct (data over inference — the operator sees *why* a
    gate parked, not just that it did).
    """

    clean: bool
    evidence: dict[str, Any] = field(default_factory=dict)
    misses: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.clean:
            return "all clean-signal conjuncts hold"
        return "; ".join(self.misses)


def _stage_of(pipeline: Pipeline, step_id: str) -> Stage | None:
    for stage in pipeline.stages:
        if any(s.id == step_id for s in stage.steps):
            return stage
    return None


def _preceding_steps(stage: Stage, gate_id: str) -> list[Step]:
    """The steps in ``stage`` before ``gate_id`` — the phase's produced signals."""
    ids = [s.id for s in stage.steps]
    if gate_id not in ids:
        return []
    return stage.steps[: ids.index(gate_id)]


def _verifier_status(cycle_metrics: dict[str, Any]) -> str:
    """Map a cycle's recorded verifier metrics to a §6 verifier-result value.

    Only ``clean`` (a verifier ran and raised zero behavioral findings) satisfies
    the predicate. No ``verifier`` key means the verifier did not produce a clean
    result at runtime — dynamically skipped, or a resumed/legacy manifest that
    predates the verifier config — recorded ``not_configured`` and parked closed
    (review F-008 runtime case). A failed verifier parks the cycle upstream (it
    never reaches the gate), so a DONE cycle only ever shows clean-or-findings.
    """
    verifier = cycle_metrics.get("verifier")
    if not isinstance(verifier, dict):
        return M.VERIFIER_NOT_CONFIGURED
    if int(verifier.get("findings_total", 0) or 0) > 0:
        return M.VERIFIER_FINDINGS
    return M.VERIFIER_CLEAN


def evaluate_clean_gate(
    manifest: M.Manifest,
    pipeline: Pipeline,
    gate_step: Step,
    iteration: str | None,
    *,
    load_findings: FindingsLoader,
) -> CleanGateDecision:
    """Evaluate the strict §4.2 clean-signal predicate for one code gate.

    ``load_findings`` returns this phase's round findings + triage verdicts (the
    orchestrator reads them from the cycle's round artifacts); a raise means they
    could not be read, which fails the finding conjuncts closed. Every conjunct
    that fails is named in ``misses`` and the aggregate ``clean`` is their AND.
    """
    misses: list[str] = []

    # Reversal circuit breaker (FR-4.2): a recorded reversal disables auto-approval
    # for the remainder of the run — a fail-closed short-circuit read here so the
    # predicate can never auto-approve after a human signalled distrust. Recorded
    # as a miss (not an early return) so the evidence snapshot is still complete.
    if manifest.auto_approval_disabled:
        misses.append(
            "auto-approval disabled for the remainder of the run after a recorded "
            "human reversal (FR-4.2 circuit breaker)"
        )

    stage = _stage_of(pipeline, gate_step.id)
    if stage is None:  # defensive: a gate must live in a stage
        misses.append(f"gate {gate_step.id!r} is not in any pipeline stage")
        return CleanGateDecision(clean=False, misses=misses)

    preceding = _preceding_steps(stage, gate_step.id)

    def _recs(step_type: str) -> list[M.StepRecord]:
        out: list[M.StepRecord] = []
        for s in preceding:
            if s.type != step_type:
                continue
            rec = manifest.record(s.id, iteration)
            if rec is not None:
                out.append(rec)
        return out

    cycles = _recs("adversarial_cycle")
    acceptance = _recs("acceptance_gate")
    shells = _recs("shell")

    # --- converged in round 1 · zero escalations · verifier clean --------------
    rounds = 0
    verifier_status = M.VERIFIER_NOT_CONFIGURED
    if not cycles:
        misses.append(
            "no adversarial_cycle precedes this code gate to evaluate for "
            "convergence (FR-4.1)"
        )
    for rec in cycles:
        if rec.status != M.DONE:
            misses.append(f"cycle {rec.id!r} did not converge (status {rec.status})")
        # A human already intervened on this cycle (an escalation resolved by a
        # `--response`), so it is not a rubber-stamp candidate — park (zero
        # escalations conjunct). A live escalation parks upstream and never
        # reaches the gate; this catches the escalation-then-resolved history.
        if rec.human_responses:
            misses.append(
                f"cycle {rec.id!r} carries a human escalation/response — not a "
                "clean-signal gate (FR-4.1 zero escalations)"
            )
        cyc_rounds = int((rec.metrics or {}).get("rounds", 0) or 0)
        rounds = max(rounds, cyc_rounds)
        if cyc_rounds != 1:
            misses.append(
                f"cycle {rec.id!r} did not converge in round 1 (rounds={cyc_rounds})"
            )
        status = _verifier_status(rec.metrics or {})
        # Aggregate to the worst status across cycles (clean only if all clean).
        if verifier_status == M.VERIFIER_NOT_CONFIGURED or status != M.VERIFIER_CLEAN:
            verifier_status = status
        if status != M.VERIFIER_CLEAN:
            misses.append(
                f"cycle {rec.id!r} verifier did not run clean (verifier={status}); "
                "a phase using auto_when_clean requires a clean verifier result "
                "(FR-4.1, review F-008)"
            )

    # --- zero blocking/major legitimate findings · zero reviewer mutations -----
    blocking = major = reviewer_mutations = 0
    try:
        findings, verdicts = load_findings()
    except Exception as exc:  # fail closed: cannot prove the findings are clean
        misses.append(
            f"could not read this phase's findings/triage artifacts to prove zero "
            f"blocking/major legitimate findings ({exc}); failing closed (FR-4.1)"
        )
        findings, verdicts = [], []
    else:
        severity_by_id = {f.get("id"): f.get("severity") for f in findings}
        verdict_by_id = {v.get("finding_id"): v.get("verdict") for v in verdicts}
        for fid, severity in severity_by_id.items():
            if fid and _MUTATION_MARKER in str(fid):
                reviewer_mutations += 1
            if (
                verdict_by_id.get(fid) == _LEGITIMATE_VERDICT
                and severity in _BLOCKING_SEVERITIES
            ):
                if severity == "blocking":
                    blocking += 1
                else:
                    major += 1
        if blocking or major:
            misses.append(
                f"{blocking} blocking + {major} major legitimate finding(s) at the "
                "gate (FR-4.1)"
            )
        if reviewer_mutations:
            misses.append(
                f"{reviewer_mutations} reviewer worktree mutation(s) recorded "
                "(FR-9.6) — the read-only contract was violated; a human must "
                "review (FR-4.1)"
            )

    # --- acceptance gate passed · tests green ----------------------------------
    # Both are guaranteed DONE by the stage walk reaching this gate (a park/fail
    # upstream stops the walk before here), but we assert them explicitly so a
    # bypassed/hand-built pipeline fails closed and the snapshot is truthful.
    acceptance_result = "pass"
    if any(rec.status != M.DONE for rec in acceptance):
        acceptance_result = "fail"
        misses.append("acceptance gate did not pass (FR-3.2)")
    elif not acceptance:
        acceptance_result = "not_run"
    tests_result = "passed"
    if any(rec.status != M.DONE for rec in shells):
        tests_result = "failed"
        misses.append("phase tests are not green")
    elif not shells:
        tests_result = "not_run"

    evidence: dict[str, Any] = {
        "rounds": rounds,
        "blocking": blocking,
        "major": major,
        "escalations": 0,
        "reviewer_mutations": reviewer_mutations,
        "acceptance_gate": acceptance_result,
        "verifier": verifier_status,
        "tests": tests_result,
    }
    return CleanGateDecision(clean=not misses, evidence=evidence, misses=misses)

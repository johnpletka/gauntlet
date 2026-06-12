"""Judge core: the decision ladder + audit, independent of HTTP framing.

Kept separate from the FastAPI layer so the full ladder (policy fast path →
LLM classifier → fail-closed) is unit-testable without a running server.
"""

from __future__ import annotations

import time
from pathlib import Path

from gauntlet.judge.classifier import LLMClassifier
from gauntlet.judge.decision import JudgeDecision
from gauntlet.judge.policy import PolicyEngine
from gauntlet.logging.redact import RedactingWriter


class JudgeCore:
    def __init__(
        self,
        policy_engine: PolicyEngine,
        *,
        classifier: LLMClassifier | None = None,
        audit_path: Path | None = None,
        writer: RedactingWriter | None = None,
        repo_root: Path | None = None,
    ) -> None:
        self.policy_engine = policy_engine
        self.classifier = classifier
        self.audit_path = audit_path
        self.writer = writer or RedactingWriter()
        # The authoritative repo boundary (BOOTSTRAP-NOTES #29/#31): when the
        # engine starts the judge it knows the real repo root and pins it here,
        # so path checks NEVER depend on the agent's per-call cwd reaching the
        # hook. The request-supplied repo_root is a fallback only (the dev
        # `gauntlet judge serve` with no --repo-root).
        self.repo_root = repo_root

    def decide(
        self,
        tool_name: str,
        tool_input: dict,
        *,
        repo_root: Path,
        run_id: str | None = None,
        step_id: str | None = None,
    ) -> JudgeDecision:
        start = time.monotonic()
        effective_root = self.repo_root or repo_root
        decision = self._ladder(tool_name, tool_input, effective_root)
        latency_ms = round((time.monotonic() - start) * 1000, 2)
        self._audit(
            tool_name, tool_input, decision, latency_ms, run_id, step_id,
            repo_root=effective_root,
        )
        return decision

    def _ladder(
        self, tool_name: str, tool_input: dict, repo_root: Path
    ) -> JudgeDecision:
        # Rung 1: deterministic policy fast path.
        fast = self.policy_engine.evaluate(
            tool_name, tool_input, repo_root=repo_root
        )
        if fast is not None and fast.decision in ("allow", "deny"):
            return fast
        # Rung 2: LLM classifier (for `ask` and unmatched).
        if self.classifier is not None:
            return self.classifier.classify(tool_name, tool_input)
        # Rung 3: fail-closed — no classifier configured, do not allow blindly.
        return JudgeDecision(
            decision="deny",
            source="fail-closed",
            rationale=(
                "command not resolved by policy and no LLM classifier "
                "configured; failing closed"
            ),
            risk_category=(fast.risk_category if fast is not None else None),
        )

    def _audit(
        self,
        tool_name: str,
        tool_input: dict,
        decision: JudgeDecision,
        latency_ms: float,
        run_id: str | None,
        step_id: str | None,
        repo_root: Path | None = None,
    ) -> None:
        if self.audit_path is None:
            return
        # FR-7.5: every decision, with source/latency/rationale. Written
        # through the redacting writer (review F-005) since these logs target
        # git. A monotonic-derived latency is included; no wall-clock stamp is
        # added here (callers stamp if they need one) to keep the core pure.
        self.writer.append_jsonl(
            self.audit_path,
            {
                "run_id": run_id,
                "step_id": step_id,
                "tool_name": tool_name,
                "tool_input": tool_input,
                "decision": decision.decision,
                "source": decision.source,
                "risk_category": decision.risk_category,
                "matched_rule": decision.matched_rule,
                "rationale": decision.rationale,
                # The boundary used for path checks — logged so a wrong root is
                # diagnosable from the audit alone, not inferred (#31).
                "repo_root": str(repo_root) if repo_root is not None else None,
                "latency_ms": latency_ms,
            },
        )

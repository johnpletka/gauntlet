"""Judge core: the decision ladder + audit, independent of HTTP framing.

Kept separate from the FastAPI layer so the full ladder (policy fast path →
LLM classifier → fail-closed) is unit-testable without a running server.
"""

from __future__ import annotations

import hashlib
import json
import threading
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
        # --- per-run allow-decision cache (FR-12.1) --------------------------
        # Maps a canonical decision key → (allow decision, its decision id). ONLY
        # `allow` outcomes are cached; `deny`/`ask` always re-evaluate, so the
        # cache can never fail open. It lives on the JudgeCore instance, which is
        # built per run (runner.build_core) — the cache dies with the run and is
        # never persisted, so a repeated identical call cannot become more
        # dangerous than its first evaluation under the same policy (§7). The lock
        # guards the cache + the decision counter because concurrent triage
        # (FR-9.1) can drive concurrent judge requests through the FastAPI layer.
        self._allow_cache: dict[str, tuple[JudgeDecision, str]] = {}
        self._decision_seq = 0
        self._lock = threading.Lock()

    def decide(
        self,
        tool_name: str,
        tool_input: dict,
        *,
        repo_root: Path,
        run_id: str | None = None,
        step_id: str | None = None,
        agent_profile: str | None = None,
    ) -> JudgeDecision:
        start = time.monotonic()
        effective_root = self.repo_root or repo_root
        key = self._cache_key(
            tool_name, tool_input, effective_root, step_id, agent_profile
        )
        with self._lock:
            self._decision_seq += 1
            decision_id = f"d{self._decision_seq}"
            hit = self._allow_cache.get(key)
        if hit is not None:
            # FR-12.1/FR-12.2: a byte-identical repeated call under an identical
            # policy is answered from the cached allow in fast-path time — the
            # classifier (LLM rung) is NOT consulted again. Recorded with this
            # call's own id AND the original decision's id so the audit shows the
            # reuse, `cached: true`.
            cached_decision, original_id = hit
            latency_ms = round((time.monotonic() - start) * 1000, 2)
            self._audit(
                tool_name, tool_input, cached_decision, latency_ms, run_id,
                step_id, repo_root=effective_root, decision_id=decision_id,
                cached=True, cached_from=original_id,
            )
            return cached_decision
        decision = self._ladder(tool_name, tool_input, effective_root, step_id)
        latency_ms = round((time.monotonic() - start) * 1000, 2)
        # Cache ONLY allow decisions (FR-12.1); deny/ask always re-evaluate.
        if decision.decision == "allow":
            with self._lock:
                self._allow_cache.setdefault(key, (decision, decision_id))
        self._audit(
            tool_name, tool_input, decision, latency_ms, run_id, step_id,
            repo_root=effective_root, decision_id=decision_id,
        )
        return decision

    def _cache_key(
        self, tool_name: str, tool_input: dict, repo_root: Path,
        step_id: str | None, agent_profile: str | None,
    ) -> str:
        """SHA-256 over the decision's full input surface (FR-12.1 / §6).

        The PRD §6 key is ``sha256(tool_name ‖ canonical_json(payload) ‖
        repo_root ‖ sha256(policy.yaml) ‖ agent_profile)``. Two hardening notes,
        both fail-closed:

        * ``in_pipeline_step`` (``bool(step_id)``) is folded in because the policy
          decision provably depends on it — ``pipeline_step_only`` deny rules
          (FR-9.8) gate an action for in-run agents that is allowed in the
          operator's own session. Keying without it could serve a cached *allow*
          for a call that would *deny* in the other context — a fail-open the §2
          "fail closed" rule forbids. This is a strict superset of the §6 key
          (every §6 element is still present), added as a soundness discriminator,
          not an artifact deviation.
        * the policy hash is derived from the in-effect policy model, so ANY policy
          change rotates the key (§7) even if the judge reloaded mid-process.
        """
        payload = json.dumps(
            tool_input, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        parts = "".join([
            tool_name,
            payload,
            str(repo_root),
            self._policy_hash(),
            agent_profile or "",
            "1" if step_id else "0",  # in_pipeline_step (see docstring)
        ])
        return hashlib.sha256(parts.encode("utf-8")).hexdigest()

    def _policy_hash(self) -> str:
        """Content hash of the in-effect policy (FR-12.1 / §7 key rotation).

        Derived from the policy model's canonical serialization rather than the
        raw file bytes so it reflects the policy actually in force on this core;
        any rule add/remove/edit changes the serialization and rotates every cache
        key immediately, so a policy change can never be masked by the cache."""
        payload = self.policy_engine.policy.model_dump(mode="json")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _ladder(
        self, tool_name: str, tool_input: dict, repo_root: Path,
        step_id: str | None = None,
    ) -> JudgeDecision:
        # Rung 1: deterministic policy fast path. step_id lets context-aware
        # rules (pipeline_step_only) gate in-run agents differently from the
        # operator's interactive session.
        fast = self.policy_engine.evaluate(
            tool_name, tool_input, repo_root=repo_root, step_id=step_id
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
        decision_id: str | None = None,
        cached: bool = False,
        cached_from: str | None = None,
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
                # Per-decision id + cache provenance (FR-12.1): a cache hit records
                # its own id, `cached: true`, and the ORIGINAL decision's id so the
                # reuse is auditable back to the evaluation that produced it.
                "decision_id": decision_id,
                "cached": cached,
                "cached_from": cached_from,
                "run_id": run_id,
                "step_id": step_id,
                "tool_name": tool_name,
                "tool_input": tool_input,
                "decision": decision.decision,
                "source": decision.source,
                "risk_category": decision.risk_category,
                "matched_rule": decision.matched_rule,
                "rationale": decision.rationale,
                # Judge LLM-rung spend, when this decision consulted the
                # classifier — the cross-process channel that carries judge cost
                # back to the manifest for `gauntlet report` (review F-003). A
                # cache HIT incurred NO LLM call, so it records no usage — else
                # `_merge_judge_usage` (which sums this field across audit lines)
                # would double-count the original evaluation's spend (FR-12.1).
                "usage": None if cached else decision.usage,
                # The boundary used for path checks — logged so a wrong root is
                # diagnosable from the audit alone, not inferred (#31).
                "repo_root": str(repo_root) if repo_root is not None else None,
                "latency_ms": latency_ms,
            },
        )

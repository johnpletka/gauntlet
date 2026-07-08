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
from gauntlet.judge.hook_client import PROBE_STEP_PREFIX
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
        # --- verifier hook-loading probe observations (review F-001) ----------
        # Nonces the judge has seen carried on a probe ``step_id`` — i.e. proof
        # that a real claude-code turn LOADED and FIRED the PreToolUse hook, which
        # then reached this judge. The verifier's :func:`verify.confirm_hook_loaded`
        # writes one nonce per probe and queries it back through ``/observed``;
        # a nonce the judge never saw means the hook did not fire, so the probe
        # parks closed rather than run the verifier unhooked. Bounded by
        # construction: only probe-prefixed step_ids are recorded, and there is one
        # probe per verifier sub-step. Guarded by the same lock as the cache.
        self._observed_probes: set[str] = set()
        # --- per-step boundary registry (PR #59 review B1) ---------------------
        # step_id → (boundary root, unregister key). The engine registers the
        # verifier's disposable-copy root against the verifier's step_id BEFORE
        # launching it; requests carrying that step_id are then confined to the
        # boundary (reads, writes, Bash paths, network, git refs — see
        # PolicyEngine.confinement_deny) and the boundary REPLACES the pinned
        # repo_root as the effective root, so the real run worktree is outside
        # the allowed surface. Server-authoritative: the agent's env can move
        # only the request-side fallback, never this registry. Registration is
        # ONE-SHOT per step_id and clearing requires the engine-held key, so a
        # verifier holding the run token cannot widen or drop its own boundary.
        self._boundaries: dict[str, tuple[Path, str]] = {}
        self._decision_seq = 0
        self._lock = threading.Lock()

    def register_boundary(self, step_id: str, root: Path, key: str) -> bool:
        """One-shot: bind ``step_id`` to a confinement ``root`` (PR #59 B1).

        Returns False (refusing) when the step_id is already bound to a
        different root/key — first write wins, so a sandboxed agent cannot
        re-register itself wider. Re-registering identical values is an
        idempotent success (resume-safe)."""
        if not step_id or not key:
            return False
        entry = (Path(root), key)
        with self._lock:
            existing = self._boundaries.get(step_id)
            if existing is not None:
                return existing == entry
            self._boundaries[step_id] = entry
            return True

    def clear_boundary(self, step_id: str, key: str) -> bool:
        """Remove a boundary — only with the key its registration returned."""
        with self._lock:
            existing = self._boundaries.get(step_id)
            if existing is None or existing[1] != key:
                return False
            del self._boundaries[step_id]
            return True

    def boundary_for(self, step_id: str | None) -> Path | None:
        if not step_id:
            return None
        with self._lock:
            entry = self._boundaries.get(step_id)
        return entry[0] if entry else None

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
        # A hook-loading probe (review F-001): record the nonce as observed as
        # early as possible — before the ladder, and regardless of the eventual
        # allow/deny — because merely REACHING the judge on a probe-tagged call is
        # the evidence the probe needs (the hook fired). The recorded set is what
        # ``/observed`` reports back to :func:`verify.confirm_hook_loaded`.
        if step_id and step_id.startswith(PROBE_STEP_PREFIX):
            with self._lock:
                self._observed_probes.add(step_id[len(PROBE_STEP_PREFIX):])
        # A registered per-step boundary (the verifier's disposable copy) WINS
        # over the pinned repo root: the pinned root is the run's authoritative
        # boundary for ordinary steps, but for a boundary-registered step the
        # copy IS the allowed world and the run worktree must be outside it
        # (PR #59 review B1 — previously the env-repointed copy root only fed
        # the request fallback, which the pinned root always overrode, leaving
        # the copy confinement inert in production runs).
        boundary = self.boundary_for(step_id)
        effective_root = boundary or self.repo_root or repo_root
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
        decision = self._ladder(
            tool_name, tool_input, effective_root, step_id, boundary=boundary
        )
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

    def observed_probe(self, nonce: str) -> bool:
        """True iff a PreToolUse call tagged with ``nonce`` has reached this judge
        (review F-001) — i.e. a real claude-code turn loaded and fired the hook.
        Backs the ``/observed`` endpoint the verifier probe queries."""
        with self._lock:
            return nonce in self._observed_probes

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
        step_id: str | None = None, boundary: Path | None = None,
    ) -> JudgeDecision:
        # Rung 0: boundary confinement (PR #59 B1). For a boundary-registered
        # step (the verifier in its disposable copy), a terminal deny fires
        # BEFORE any policy allow can bless the call: outside-boundary paths
        # (reads included), default-deny network, ref-mutating git. Falls
        # through with the boundary as the effective root, so the normal
        # ladder's path rules (write-outside-repo, credential-outside-repo)
        # also judge against the copy, not the run worktree.
        if boundary is not None:
            confined = self.policy_engine.confinement_deny(
                tool_name, tool_input, boundary=boundary
            )
            if confined is not None:
                return confined
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

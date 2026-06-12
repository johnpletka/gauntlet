"""Assemble and serve the judge (the `gauntlet judge serve` dev command, P2).

The engine manages the judge lifecycle in P3; here it is a standalone dev
command so P2 can drive real CLIs against a live judge.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from gauntlet.judge.classifier import LLMClassifier
from gauntlet.judge.core import JudgeCore
from gauntlet.judge.policy import Policy, PolicyEngine
from gauntlet.judge.service import TOKEN_ENV_VAR, create_app, token_from_env

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787

# The classifier rung must answer well within the CLI hook timeout
# (gauntlet-judge-hook uses 8 s), so it is bounded below that (review F-007).
# The named `judge_llm` agent profile (FR-2) is wired by the P3 config loader;
# until then the dev command binds a bounded ad-hoc ApiAdapter here.
JUDGE_LLM_TIMEOUT_S = 6.5  # single attempt < the 8 s hook timeout; first-call
# warmup measured 4.9 s on gpt-5-mini (steady-state 2.3-3.1 s) — 5.0 s left no
# headroom and a cold judge would fail-close the builder's first write (#26).
JUDGE_LLM_MAX_TOKENS = 512
# Reasoning effort for the classifier rung: "minimal" measured 2.3-3.1 s/verdict
# on gpt-5-mini AND was more rubric-faithful than "low" (which allowed an
# out-of-repo write in the probe). Verified live 2026-06-12; notes #26.
JUDGE_LLM_REASONING_EFFORT = "minimal"


def build_core(
    *,
    policy_path: Path,
    audit_path: Path | None = None,
    judge_model: str | None = None,
) -> JudgeCore:
    engine = PolicyEngine(Policy.load(policy_path))
    classifier = None
    if judge_model:
        from gauntlet.adapters.api import ApiAdapter

        classifier = LLMClassifier(
            ApiAdapter(
                model=judge_model,
                timeout_s=JUDGE_LLM_TIMEOUT_S,
                max_tokens=JUDGE_LLM_MAX_TOKENS,
                # No temperature: gpt-5-family models reject any non-default
                # value, and litellm's UnsupportedParamsError made EVERY
                # classifier rung fail closed — verified live and pinned
                # (notes #26). Determinism comes from the rubric, not temp=0.
                # "minimal" reasoning keeps verdicts at ~2-3 s — inside the
                # 5 s single attempt that stays under the 8 s hook timeout
                # (review F-007 round 2; default effort blew the budget).
                reasoning_effort=JUDGE_LLM_REASONING_EFFORT,
                # A schema-invalid answer fails closed to deny rather than
                # burning the timeout on retries.
                max_schema_retries=0,
            )
        )
    return JudgeCore(engine, classifier=classifier, audit_path=audit_path)


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def serve(
    *,
    policy_path: Path,
    audit_path: Path | None = None,
    judge_model: str | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    token: str | None = None,
) -> None:  # pragma: no cover - exercised via the live contract suite
    import os

    import uvicorn

    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(
            f"judge refuses to bind non-loopback host {host!r} (§8: localhost only)"
        )
    resolved_token = token or os.environ.get(TOKEN_ENV_VAR) or generate_token()
    os.environ[TOKEN_ENV_VAR] = resolved_token
    core = build_core(
        policy_path=policy_path, audit_path=audit_path, judge_model=judge_model
    )
    app = create_app(core, token=resolved_token)
    print(f"gauntlet judge listening on http://{host}:{port}")
    print(f"{TOKEN_ENV_VAR}={resolved_token}")
    uvicorn.run(app, host=host, port=port, log_level="warning")


__all__ = ["build_core", "generate_token", "serve", "token_from_env"]

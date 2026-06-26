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
    repo_root: Path | None = None,
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
    return JudgeCore(
        engine, classifier=classifier, audit_path=audit_path, repo_root=repo_root
    )


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def classifier_status_message(
    judge_model: str | None, *, resolve_error: str | None = None
) -> str:
    """One-line startup status for the judge's LLM classifier rung (FR-7.2).

    Emitted loudly (stderr) at startup. Three states, because both an ABSENT and
    an UNRESOLVABLE model fail the classifier closed on every command the
    ``policy.yaml`` fast-path does not match — and "enabled" must never be
    printed for a model that will not actually answer (PR #13 review: a typo like
    ``claude-heroku`` was announced as enabled, then failed closed on every
    call). ``resolve_error`` is the message from :func:`model_provider_error`;
    ``None`` means resolvable (or unverifiable). Silently failing closed is
    exactly what "data over inference" (CLAUDE.md §2) warns against."""
    if not judge_model:
        return (
            "judge: WARNING — no --judge-model set; LLM classifier DISABLED. "
            "Commands not matched by the policy.yaml fast-path allow/deny rules "
            "(and every 'ask' rule) will FAIL CLOSED (deny). "
            "Pass --judge-model <litellm-model-id> to enable classification."
        )
    if resolve_error:
        return (
            f"judge: WARNING — judge-model {judge_model!r} is SET but NOT "
            f"resolvable by LiteLLM ({resolve_error}); the LLM classifier will "
            "FAIL CLOSED on every command the policy.yaml fast-path does not "
            "match. Use a valid LiteLLM model id."
        )
    return f"judge: LLM classifier enabled ({judge_model})"


def serve(
    *,
    policy_path: Path,
    audit_path: Path | None = None,
    judge_model: str | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    token: str | None = None,
    repo_root: Path | None = None,
    run_id: str | None = None,
) -> None:  # pragma: no cover - exercised via the live contract suite
    import os
    import sys

    import uvicorn

    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(
            f"judge refuses to bind non-loopback host {host!r} (§8: localhost only)"
        )
    resolved_token = token or os.environ.get(TOKEN_ENV_VAR) or generate_token()
    os.environ[TOKEN_ENV_VAR] = resolved_token
    core = build_core(
        policy_path=policy_path, audit_path=audit_path, judge_model=judge_model,
        repo_root=repo_root,
    )
    app = create_app(core, token=resolved_token, expected_run_id=run_id)
    print(f"gauntlet judge listening on http://{host}:{port}")
    print(f"{TOKEN_ENV_VAR}={resolved_token}")
    # Resolve the model the SAME way doctor does, so "enabled" is never printed
    # for an id that will fail every classify call closed (PR #13 review).
    resolve_error = None
    if judge_model:
        from gauntlet.adapters.api import model_provider_error

        resolve_error = model_provider_error(judge_model)
    # Classifier state goes to stderr so it never contaminates the token line on
    # stdout (which tooling/operators scrape) — but is still impossible to miss.
    print(
        classifier_status_message(judge_model, resolve_error=resolve_error),
        file=sys.stderr,
    )
    uvicorn.run(app, host=host, port=port, log_level="warning")


__all__ = [
    "build_core",
    "classifier_status_message",
    "generate_token",
    "serve",
    "token_from_env",
]

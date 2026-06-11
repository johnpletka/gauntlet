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

        classifier = LLMClassifier(ApiAdapter(model=judge_model))
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

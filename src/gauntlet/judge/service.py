"""Localhost judge service (FR-7.1, §8).

FastAPI app exposing `/decide` and `/healthz`. Binds 127.0.0.1 only; a per-run
shared token (sent as `X-Gauntlet-Token`) rejects foreign callers. The app
wraps a :class:`JudgeCore`; HTTP is only framing.
"""

from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from gauntlet.judge.core import JudgeCore

TOKEN_ENV_VAR = "GAUNTLET_JUDGE_TOKEN"
TOKEN_HEADER = "X-Gauntlet-Token"


class DecideRequest(BaseModel):
    tool_name: str
    tool_input: dict[str, Any] = Field(default_factory=dict)
    repo_root: str
    run_id: str | None = None
    step_id: str | None = None


class DecideResponse(BaseModel):
    decision: str
    source: str
    rationale: str
    risk_category: str | None = None
    matched_rule: str | None = None


def create_app(core: JudgeCore, *, token: str) -> FastAPI:
    app = FastAPI(title="gauntlet-judge", docs_url=None, redoc_url=None)

    def _check_token(supplied: str | None) -> None:
        # constant-time compare; reject foreign callers (§8)
        if not supplied or not hmac.compare_digest(supplied, token):
            raise HTTPException(status_code=401, detail="bad or missing judge token")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        # Unauthenticated liveness check so hook clients can distinguish
        # judge-down from judge-deny (review F-004).
        return {"status": "ok"}

    @app.post("/decide", response_model=DecideResponse)
    def decide(
        req: DecideRequest,
        x_gauntlet_token: str | None = Header(default=None, alias=TOKEN_HEADER),
    ) -> DecideResponse:
        _check_token(x_gauntlet_token)
        decision = core.decide(
            req.tool_name,
            req.tool_input,
            repo_root=Path(req.repo_root),
            run_id=req.run_id,
            step_id=req.step_id,
        )
        return DecideResponse(
            decision=decision.decision,
            source=decision.source,
            rationale=decision.rationale,
            risk_category=decision.risk_category,
            matched_rule=decision.matched_rule,
        )

    return app


def token_from_env() -> str:
    token = os.environ.get(TOKEN_ENV_VAR)
    if not token:
        raise RuntimeError(
            f"{TOKEN_ENV_VAR} is not set; the judge refuses to start without a "
            "per-run shared token (§8 foreign-caller rejection)"
        )
    return token

"""Console FastAPI app (P1, FR-10.4 bootstrap posture).

A sibling of ``judge/service.py`` with the same loopback + constant-time-token
shape. P1 keeps the judge's simple token delivery — the serve token is accepted
in the ``X-Gauntlet-Token`` header **or** a ``?token=`` query param (so a browser
can navigate with it) — and ``/healthz`` stays unauthenticated. The full
``/login`` httpOnly-cookie + CSRF flow (FR-10.4/10.6) is deliberately deferred to
P7; earlier phases must not pre-build it.

The app is a thin HTTP shell over :class:`RunStore`: read endpoints under
``/api`` and two server-rendered Jinja pages (run list, run detail). No live
updates yet (P2), no control verbs (P3+), no gate resolution (P5).
"""

from __future__ import annotations

import contextlib
import hmac
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from gauntlet.web.sse import SSE_HEADERS, event_stream, log_tail_stream
from gauntlet.web.store import RunNotFound, RunStore, UnsafePath
from gauntlet.web.views import register_views
from gauntlet.web.watcher import Watcher

TOKEN_ENV_VAR = "GAUNTLET_WEB_TOKEN"
TOKEN_HEADER = "X-Gauntlet-Token"
TOKEN_QUERY = "token"

_WEB_DIR = Path(__file__).resolve().parent
STATIC_DIR = _WEB_DIR / "static"


def _token_dependency(token: str):
    """Build the per-app auth dependency (constant-time, header-or-query).

    Mirrors the judge's foreign-caller rejection (constant-time
    :func:`hmac.compare_digest`). Accepts the token from the ``X-Gauntlet-Token``
    header (API-client parity with the judge) or the ``?token=`` query param (so
    a browser can carry it in a link) — the P1 bootstrap delivery only; P7
    replaces the query path with the ``/login`` cookie exchange.
    """

    def check(
        x_gauntlet_token: str | None = Header(default=None, alias=TOKEN_HEADER),
        token_q: str | None = Query(default=None, alias=TOKEN_QUERY),
    ) -> None:
        supplied = x_gauntlet_token or token_q
        if not supplied or not hmac.compare_digest(supplied, token):
            raise HTTPException(status_code=401, detail="bad or missing web token")

    return check


def create_app(
    store: RunStore, *, token: str, watcher: Watcher | None = None
) -> FastAPI:
    # The watcher (P2) drives live state. Created here unless injected (tests
    # inject one they drive synchronously via `poll_once`). It is started/stopped
    # by the ASGI lifespan — so `TestClient(app)` used *without* `with` (the P1
    # read-only tests) never starts the poll loop, while `with TestClient(app)`
    # gets a live watcher.
    watcher = watcher if watcher is not None else Watcher(store)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        watcher.start()
        try:
            yield
        finally:
            await watcher.stop()

    app = FastAPI(
        title="gauntlet-console", docs_url=None, redoc_url=None, lifespan=lifespan
    )
    app.state.watcher = watcher
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    auth = Depends(_token_dependency(token))

    # Fail-closed error mapping: a bad path segment is a 400 (the caller asked
    # for something unsafe), a missing run/slug/step is a 404.
    @app.exception_handler(UnsafePath)
    async def _unsafe(_request: Request, exc: UnsafePath) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(RunNotFound)
    async def _missing(_request: Request, exc: RunNotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        # Unauthenticated liveness probe, like the judge's /healthz, so the P7
        # console registry can distinguish "console down" from "wrong token".
        return {"status": "ok"}

    # ---- JSON read API (§6) -------------------------------------------------
    @app.get("/api/runs", dependencies=[auth])
    def api_runs() -> list[dict]:
        return [r.model_dump() for r in store.list_rows()]

    @app.get("/api/runs/{slug}", dependencies=[auth])
    def api_run(slug: str, run_id: str | None = Query(default=None)) -> dict:
        man = store.manifest(slug, run_id)
        # The full manifest is the §6 contract; resume_intel is P5, not here.
        body = man.model_dump(mode="json")
        body["owned"] = False  # no supervisor in P1
        return body

    @app.get("/api/runs/{slug}/steps/{step}", dependencies=[auth])
    def api_step(
        slug: str, step: str, run_id: str | None = Query(default=None)
    ) -> dict:
        return store.step_detail(slug, step, run_id).model_dump()

    # ---- step log tail (P2, FR-3.2) -----------------------------------------
    @app.get("/api/runs/{slug}/steps/{step}/log", dependencies=[auth])
    def api_step_log(
        slug: str,
        step: str,
        run_id: str | None = Query(default=None),
        from_: int = Query(default=0, alias="from", ge=0),
        name: str | None = Query(default=None),
    ) -> dict:
        return store.step_log(
            slug, step, run_id=run_id, name=name, offset=from_
        ).model_dump()

    @app.get("/api/runs/{slug}/steps/{step}/log/stream", dependencies=[auth])
    async def api_step_log_stream(
        request: Request,
        slug: str,
        step: str,
        run_id: str | None = Query(default=None),
        from_: int = Query(default=0, alias="from", ge=0),
        name: str | None = Query(default=None),
    ) -> StreamingResponse:
        gen = log_tail_stream(
            store,
            slug,
            step,
            run_id=run_id,
            name=name,
            start=from_,
            is_disconnected=request.is_disconnected,
        )
        return StreamingResponse(
            gen, media_type="text/event-stream", headers=SSE_HEADERS
        )

    # ---- live state SSE (P2, FR-8.2) ----------------------------------------
    @app.get("/events", dependencies=[auth])
    async def events(request: Request) -> StreamingResponse:
        gen = event_stream(store, watcher, is_disconnected=request.is_disconnected)
        return StreamingResponse(
            gen, media_type="text/event-stream", headers=SSE_HEADERS
        )

    # ---- server-rendered pages + live partials (FR-1/FR-2/FR-8) -------------
    register_views(app, store, auth)

    return app


__all__ = ["create_app", "TOKEN_ENV_VAR", "TOKEN_HEADER", "TOKEN_QUERY"]

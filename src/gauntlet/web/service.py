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
from pydantic import BaseModel, field_validator

from gauntlet.engine.run import UnsafeRunSegment, safe_run_segment
from gauntlet.web.sse import SSE_HEADERS, event_stream, log_tail_stream
from gauntlet.web.store import RunNotFound, RunStore, UnsafePath
from gauntlet.web.supervisor import AbortFailed, AbortRefused
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


class LaunchBody(BaseModel):
    """``POST /api/runs`` body (§6 control surface)."""

    slug: str
    pipeline: str | None = None
    no_judge: bool = False

    @field_validator("slug")
    @classmethod
    def _slug_is_a_safe_segment(cls, value: str) -> str:
        # Containment at the body boundary (FR-10.1 / review F-001): the slug
        # becomes a run-root path segment, so reject a traversal/separator/NUL
        # segment before it ever reaches the supervisor's path construction.
        return safe_run_segment(value, kind="slug")


def create_app(
    store: RunStore,
    *,
    token: str,
    watcher: Watcher | None = None,
    supervisor=None,
) -> FastAPI:
    # The watcher (P2) drives live state. Created here unless injected (tests
    # inject one they drive synchronously via `poll_once`). It is started/stopped
    # by the ASGI lifespan — so `TestClient(app)` used *without* `with` (the P1
    # read-only tests) never starts the poll loop, while `with TestClient(app)`
    # gets a live watcher.
    watcher = watcher if watcher is not None else Watcher(store)
    # The supervisor (P3) owns console-launched runs and surfaces the worktree
    # lock. Wire it into the store so list rows carry the owned/observed/external
    # badge (FR-1.4/FR-10.5). When absent (read-only deployments) every run is
    # observed and the control endpoints fail closed with 503.
    if supervisor is not None:
        store.supervisor = supervisor

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        # P4 (FR-7.1/7.3): before serving, re-discover owned runs from disk and
        # reconcile orphans — a restarted console holds no authoritative run
        # state (D2). Guarded so read-only deployments and the minimal test
        # stubs (no `reattach`) are untouched. Fail CLOSED (review F-001): a
        # re-discovery failure must NOT let the console come up pretending the
        # reattach pass completed while serving stale ownership state — let it
        # propagate so startup aborts. Per-run recovery is best-effort *inside*
        # JobSupervisor.reattach() (one unreconcilable run is logged and
        # skipped); an exception reaching here means the scan itself failed, and
        # silently swallowing it would violate the fail-closed/process-fidelity
        # guidance and skip P4's required reattach pass.
        reattach = getattr(supervisor, "reattach", None)
        if callable(reattach):
            reattach()
        watcher.start()
        try:
            yield
        finally:
            await watcher.stop()

    app = FastAPI(
        title="gauntlet-console", docs_url=None, redoc_url=None, lifespan=lifespan
    )
    app.state.watcher = watcher
    app.state.supervisor = supervisor
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

    # An unsafe slug/run-id reaching the control path (engine/supervisor) is a
    # 400 — the caller asked for an out-of-tree segment (FR-10.1 / review F-001).
    @app.exception_handler(UnsafeRunSegment)
    async def _unsafe_segment(_request: Request, exc: UnsafeRunSegment) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    # Abort fails closed: refused (not owned/live/non-terminal) → 404/409 from
    # the exception (review F-002); the sanctioned child failing → 502 so a
    # failed destructive action never reads as success (review F-003).
    @app.exception_handler(AbortRefused)
    async def _abort_refused(_request: Request, exc: AbortRefused) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    @app.exception_handler(AbortFailed)
    async def _abort_failed(_request: Request, exc: AbortFailed) -> JSONResponse:
        return JSONResponse(status_code=502, content={"detail": str(exc)})

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
        # owned/observed badge now real (FR-1.4); external = a live foreign
        # driver holds the worktree lock (FR-10.5).
        owned, attached, external = store._ownership(
            slug, man.run_id, store.worktree_lock()
        )
        body["owned"] = owned
        body["attached"] = attached
        body["external"] = external
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

    # ---- owned-run captured log tail (P3, FR-3.3) ---------------------------
    @app.get("/api/runs/{slug}/serve-log", dependencies=[auth])
    def api_serve_log(
        slug: str,
        run_id: str | None = Query(default=None),
        from_: int = Query(default=0, alias="from", ge=0),
    ) -> dict:
        return store.serve_log(slug, run_id=run_id, offset=from_).model_dump()

    # ---- control surface: sanctioned CLI-verb children (P3, §6) -------------
    def _require_supervisor():
        if supervisor is None:
            raise HTTPException(
                status_code=503,
                detail="run supervision is unavailable (no supervisor configured)",
            )
        return supervisor

    def _refuse_if_worktree_locked(action: str) -> None:
        """Fail fast if a live process is already driving the worktree (FR-10.5).

        The engine lock is the real enforcement (a launched child would itself
        fail closed); this just surfaces it as a clear 409 instead of a silent
        failed-launch, mirroring the UI disabling Launch/Resume/Approve.
        """
        lock = supervisor.driving_lock()
        if lock is not None and lock.live:
            holder = f"{lock.slug}/{lock.run_id}" if lock.run_id else lock.slug
            raise HTTPException(
                status_code=409,
                detail=(
                    f"cannot {action}: worktree is being driven by {holder} "
                    f"(pid {lock.pid}); wait, or abort that run first (FR-10.5)"
                ),
            )

    @app.post("/api/runs", dependencies=[auth])
    def api_launch(body: LaunchBody) -> dict:
        _require_supervisor()
        _refuse_if_worktree_locked("launch a run")
        proc = supervisor.launch_run(
            body.slug, pipeline=body.pipeline, no_judge=body.no_judge
        )
        return {
            "slug": proc.slug,
            "run_id": proc.run_id,
            "pid": proc.pid,
            "log_path": str(proc.log_path),
            "owned": True,
            "status": "launched",
        }

    @app.post("/api/runs/{slug}/abort", dependencies=[auth])
    def api_abort(slug: str) -> dict:
        _require_supervisor()
        # The supervisor fails closed: it raises AbortRefused (→404/409) for
        # observed/missing/terminal runs and AbortFailed (→502) if the
        # sanctioned `gauntlet abort` child exits non-zero or times out, so we
        # only reach here when the destructive action actually succeeded.
        rp = supervisor.abort(slug)
        try:
            status = store.manifest(slug).status
        except RunNotFound:
            status = "unknown"
        return {"slug": slug, "run_id": rp.run_id, "status": status}

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

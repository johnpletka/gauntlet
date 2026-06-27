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
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from gauntlet.engine.run import UnsafeRunSegment, safe_run_segment
from gauntlet.web.auth import (
    AUTH_COOKIE,
    AUTH_QUERY,
    QUERY_TOKEN_PARAM,
    CsrfError,
    LoginRequired,
    SessionStore,
    Unauthenticated,
    authenticate,
    enforce_csrf,
    set_session_cookie,
)
from gauntlet.web.auth import TOKEN_HEADER as TOKEN_HEADER  # re-export (tests/API)
from gauntlet.web.config import web_config_from
from gauntlet.web.gate import GateResolver, NoPendingGate, handoff_prompt
from gauntlet.web.intel import resume_intel
from gauntlet.web.notify import build_notifier
from gauntlet.web.sse import SSE_HEADERS, event_stream, log_tail_stream
from gauntlet.web.store import RunNotFound, RunStore, UnsafePath
from gauntlet.web.supervisor import ControlFailed, ControlRefused
from gauntlet.web.views import register_views
from gauntlet.web.watcher import Watcher

TOKEN_ENV_VAR = "GAUNTLET_WEB_TOKEN"

_WEB_DIR = Path(__file__).resolve().parent
STATIC_DIR = _WEB_DIR / "static"

# Path prefixes whose unauthenticated requests get a 401 (API / SSE / partials,
# all called by code), vs. browser page navigations that get a /login redirect.
_API_PREFIXES = ("/api", "/events", "/partials", "/static", "/healthz")


def _wants_login_redirect(request: Request) -> bool:
    """A browser page GET (not an API/SSE/partial fetch) → redirect to /login.

    An unauthenticated **page** navigation should land on the login form, not a
    bare 401; an unauthenticated API/SSE/partial call (always issued by code that
    can read a status) gets 401 so it fails closed loudly. The same classifier
    decides which ``?p=``-authenticated requests get the p-stripping page
    redirect (FR-2.5) vs. an in-place cookie bootstrap (FR-2.2).
    """
    if request.method not in ("GET", "HEAD"):
        return False
    return not request.url.path.startswith(_API_PREFIXES)


class QueryAuthBootstrap(Exception):
    """A ``?p=``-authenticated **page** GET → 303 to the p-stripped path (FR-2.5).

    Carries the same-path redirect target (``p`` removed, other query params
    preserved) and the freshly-minted session id so the handler can set the
    bootstrap cookie on the redirect response — the token never reaches the
    rendered page or the address bar.
    """

    def __init__(self, location: str, sid: str) -> None:
        super().__init__("query-auth bootstrap redirect")
        self.location = location
        self.sid = sid


def _make_auth_dependency(sessions: SessionStore):
    """Build the per-app auth + CSRF dependency (FR-10.4/10.6).

    Authenticates a request by login-session **cookie** (browser) or
    ``X-Gauntlet-Token`` **header** (API parity); the ``?token=`` query path of
    P1–P6 is gone (the token must never ride in a URL, FR-10.4). On a
    cookie-authenticated state-changing request it additionally enforces the
    session-bound CSRF token + same-origin (FR-10.6); header-authenticated POSTs
    are CSRF-exempt (header auth is not ambient and cannot be forged cross-site).
    """

    def check(request: Request, response: Response) -> None:
        try:
            source = authenticate(request, sessions)
        except Unauthenticated as exc:
            if _wants_login_redirect(request):
                nxt = request.url.path
                if request.url.query:
                    nxt = f"{nxt}?{request.url.query}"
                raise LoginRequired(nxt) from exc
            raise HTTPException(
                status_code=401, detail="bad or missing web token"
            ) from exc
        # A valid loopback ?p= token bootstraps a cookie session (FR-2.2). Reached
        # only when there was no valid cookie (authenticate short-circuits on the
        # cookie first, FR-2.3), so we never mint a duplicate session on reload.
        if source == AUTH_QUERY:
            sid, _csrf = sessions.create_session()
            if _wants_login_redirect(request):
                # A page navigation: redirect to the same path with `p` stripped
                # (other query params preserved) so the token leaves the address
                # bar/history; the cookie rides on the redirect (FR-2.5).
                stripped = request.url.remove_query_params(QUERY_TOKEN_PARAM)
                location = stripped.path + (
                    f"?{stripped.query}" if stripped.query else ""
                )
                raise QueryAuthBootstrap(location, sid)
            # An API/asset/state-changing call: set the cookie in place and let it
            # proceed (no redirect — the caller is code, not the address bar).
            set_session_cookie(response, sid)
        # CSRF is only required for the **ambient** cookie source; header and query
        # auth are not ambient and cannot be forged cross-site, so they are exempt
        # (FR-2.4 / FR-10.6).
        if request.method not in ("GET", "HEAD", "OPTIONS") and source == AUTH_COOKIE:
            try:
                enforce_csrf(request, sessions)
            except CsrfError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc

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


class ApproveBody(BaseModel):
    """``POST …/approve`` body (FR-4.4). Approve is non-destructive — no confirm."""

    gate: str | None = None
    notes: str | None = None


class RejectBody(BaseModel):
    """``POST …/reject`` body (FR-4.4). Notes required; confirm required (FR-10.7)."""

    notes: str
    gate: str | None = None
    confirm: bool = False


class AbortBody(BaseModel):
    """``POST …/abort`` body. Confirm required for the destructive verb (FR-10.7)."""

    confirm: bool = False


def create_app(
    store: RunStore,
    *,
    token: str,
    watcher: Watcher | None = None,
    supervisor=None,
    handoff_enabled: bool = False,
    notifier=None,
    base_url: str = "",
    notifications: bool = False,
) -> FastAPI:
    # The watcher (P2) drives live state. Created here unless injected (tests
    # inject one they drive synchronously via `poll_once`). It is started/stopped
    # by the ASGI lifespan — so `TestClient(app)` used *without* `with` (the P1
    # read-only tests) never starts the poll loop, while `with TestClient(app)`
    # gets a live watcher.
    watcher = watcher if watcher is not None else Watcher(store)
    # The notifier (P6, FR-9) hangs off the watcher's event bus, fail-soft. Built
    # from the `web.notify` config block (per-channel on/off + Slack webhook,
    # FR-9.4) only when `notifications=True` (the runner enables it for a real
    # `gauntlet serve`); it defaults **off** so the many test apps that drive a
    # live watcher via `with TestClient(app)` never fire a real desktop/Slack
    # send. Tests that exercise notifications inject a stub `notifier` instead.
    # Its in-tab channel publishes onto the watcher's SSE queues, so it is wired
    # *after* the watcher exists.
    if notifier is None and notifications:
        notifier = build_notifier(
            web_config_from(store.config).notify, watcher=watcher, base_url=base_url
        )
    watcher.notifier = notifier
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
    app.state.handoff_enabled = handoff_enabled
    # Per-serve login sessions + CSRF tokens (P7, FR-10.4/10.6). In-memory: the
    # console holds no durable auth state — a restart invalidates cookies and the
    # operator logs in again (fail-closed, like a fresh token).
    sessions = SessionStore(token)
    app.state.sessions = sessions
    gates = GateResolver(store)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    auth = Depends(_make_auth_dependency(sessions))

    # A browser GET with no valid cookie is bounced to the login form (FR-10.4),
    # preserving where it was headed via ?next= so login returns it there.
    @app.exception_handler(LoginRequired)
    async def _login_required(_request: Request, exc: LoginRequired) -> RedirectResponse:
        return RedirectResponse(
            f"/login?next={quote(exc.next_path, safe='')}", status_code=303
        )

    # A ?p=-authenticated page GET → 303 to the same path with `p` stripped,
    # carrying the bootstrap cookie so the token-free follow-up renders the page
    # (FR-2.5). The cookie is set here (not in the dependency) because the redirect
    # response is the one the browser keeps.
    @app.exception_handler(QueryAuthBootstrap)
    async def _query_bootstrap(_request: Request, exc: QueryAuthBootstrap) -> RedirectResponse:
        resp = RedirectResponse(exc.location, status_code=303)
        set_session_cookie(resp, exc.sid)
        return resp

    # Every console response carries Referrer-Policy: no-referrer (FR-2.6) so the
    # brief pre-redirect ?p= URL is never leaked as a Referer on sub-resource loads
    # or outbound links.
    @app.middleware("http")
    async def _no_referrer(request: Request, call_next):
        response = await call_next(request)
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

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

    # Control verbs fail closed: refused (not owned/live/non-terminal, missing
    # run, missing notes) → the exception's status (404/409/400, review F-002);
    # the sanctioned child failing → 502 so a failed action never reads as
    # success (review F-003). AbortRefused/AbortFailed subclass these, so the
    # base handlers cover them via MRO lookup.
    @app.exception_handler(ControlRefused)
    async def _control_refused(_request: Request, exc: ControlRefused) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    @app.exception_handler(ControlFailed)
    async def _control_failed(_request: Request, exc: ControlFailed) -> JSONResponse:
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    # No pending gate/escalation to resolve → 404 (FR-4.1/4.6).
    @app.exception_handler(NoPendingGate)
    async def _no_gate(_request: Request, exc: NoPendingGate) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        # Unauthenticated liveness probe, like the judge's /healthz, so the P7
        # console registry can distinguish "console down" from "wrong token".
        return {"status": "ok"}

    # ---- JSON read API (§6) -------------------------------------------------
    @app.get("/api/runs", dependencies=[auth])
    def api_runs(
        status: str | None = Query(default=None),
        slug: str | None = Query(default=None),
        q: str | None = Query(default=None),
        sort: str | None = Query(default=None),
    ) -> list[dict]:
        # FR-1.2: status filter, slug/free-text search, and sort, applied in the
        # read model so the JSON API and the rendered page agree.
        return [
            r.model_dump()
            for r in store.list_rows(status=status, slug=slug, q=q, sort=sort)
        ]

    @app.get("/api/runs/{slug}", dependencies=[auth])
    def api_run(slug: str, run_id: str | None = Query(default=None)) -> dict:
        man = store.manifest(slug, run_id)
        # The full manifest is the §6 contract; P5 adds the computed
        # resume_intel recovery classification (FR-5.1).
        body = man.model_dump(mode="json")
        body["resume_intel"] = resume_intel(man).model_dump()
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

    # ---- gate / escalation resolution (P5, FR-4) ----------------------------
    @app.get("/api/runs/{slug}/gate", dependencies=[auth])
    def api_gate(slug: str, run_id: str | None = Query(default=None)) -> dict:
        # Resolves a parked human_gate's show: artifacts OR a parked
        # adversarial_cycle's escalation evidence (FR-4.2/4.6). NoPendingGate →
        # 404; UnsafePath (a traversal in a show: name) → 400.
        return gates.gate(slug, run_id).model_dump()

    @app.get("/api/runs/{slug}/diff", dependencies=[auth])
    def api_diff(
        slug: str,
        run_id: str | None = Query(default=None),
        from_: str | None = Query(default=None, alias="from"),
        to: str | None = Query(default=None),
    ) -> dict:
        # Deterministic phase-diff selection when from/to omitted (FR-4.3), incl.
        # the no-committed-diff sentinel; explicit SHAs override.
        return gates.diff(slug, run_id=run_id, from_sha=from_, to_sha=to).model_dump()

    @app.get("/api/runs/{slug}/judge-audit", dependencies=[auth])
    def api_judge_audit(slug: str, run_id: str | None = Query(default=None)) -> dict:
        return {"slug": slug, "entries": store.judge_audit(slug, run_id=run_id)}

    @app.get("/api/runs/{slug}/history", dependencies=[auth])
    def api_history(slug: str) -> dict:
        # Full-history browser (FR-2.4): every run-<ts> dir of the slug, newest
        # first, so a past run is addressable read-only via ?run_id=.
        return {"slug": slug, "runs": store.run_history(slug)}

    @app.get("/api/runs/{slug}/report", dependencies=[auth])
    def api_report(slug: str, run_id: str | None = Query(default=None)) -> dict:
        # Cost breakdown (FR-2.4/§6) — the same text `gauntlet report` prints.
        return {"slug": slug, "report": store.report_text(slug, run_id=run_id)}

    @app.get("/api/runs/{slug}/handoff", dependencies=[auth])
    def api_handoff(slug: str, run_id: str | None = Query(default=None)) -> dict:
        # Opt-in scoped-analysis hand-off (FR-4.7): assemble a copy-pasteable,
        # read-only prompt. The console spawns nothing and makes no model call.
        # Off by default; 404 until enabled via config (the web: block, P7).
        if not app.state.handoff_enabled:
            raise HTTPException(
                status_code=404,
                detail="scoped-analysis hand-off is disabled (FR-4.7 opt-in)",
            )
        return handoff_prompt(gates.gate(slug, run_id)).model_dump()

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

    def _require_confirm(confirm: bool, verb: str) -> None:
        """Destructive-verb confirmation (FR-10.7).

        UX-safety against a misclick aborting/rejecting a long, expensive run —
        *not* a security control (it adds nothing against a caller who holds the
        token, FR-10.4). A POST without ``confirm: true`` fails closed."""
        if not confirm:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"`{verb}` is destructive and requires explicit confirmation "
                    "(send `confirm: true`) (FR-10.7)"
                ),
            )

    @app.post("/api/runs/{slug}/abort", dependencies=[auth])
    def api_abort(slug: str, body: AbortBody | None = None) -> dict:
        _require_supervisor()
        _require_confirm(bool(body and body.confirm), "abort")
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

    @app.post("/api/runs/{slug}/approve", dependencies=[auth])
    def api_approve(slug: str, body: ApproveBody | None = None) -> dict:
        # Approve drives the rest of the run (FR-6.2) — a driving verb, so it
        # fails closed if the worktree is already being driven (FR-10.5).
        _require_supervisor()
        _refuse_if_worktree_locked("approve a gate")
        body = body or ApproveBody()
        proc = supervisor.approve(slug, gate=body.gate, notes=body.notes)
        return {
            "slug": proc.slug,
            "run_id": proc.run_id,
            "pid": proc.pid,
            "owned": True,
            "status": "approving",
        }

    @app.post("/api/runs/{slug}/resume", dependencies=[auth])
    def api_resume(slug: str) -> dict:
        # Resume is a driving verb (FR-10.5) — fails closed under the worktree lock.
        _require_supervisor()
        _refuse_if_worktree_locked("resume a run")
        proc = supervisor.resume(slug)
        return {
            "slug": proc.slug,
            "run_id": proc.run_id,
            "pid": proc.pid,
            "owned": True,
            "status": "resuming",
        }

    @app.post("/api/runs/{slug}/reject", dependencies=[auth])
    def api_reject(slug: str, body: RejectBody) -> dict:
        # Reject fails a gate (destructive, FR-10.7) but is NOT a driving verb
        # (it takes no worktree lock — it only marks the gate failed), so it is a
        # quick, fail-closed child and needs no lock guard.
        _require_supervisor()
        _require_confirm(body.confirm, "reject")
        rp = supervisor.reject(slug, body.notes, gate=body.gate)
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

    # ---- server-rendered pages + live partials + /login (FR-1/FR-2/FR-8/10.4)
    register_views(app, store, auth, sessions)

    return app


__all__ = ["create_app", "TOKEN_ENV_VAR", "TOKEN_HEADER"]

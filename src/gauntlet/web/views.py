"""Server-rendered console pages + live partials (P1/P2/P5/P7, FR-1/FR-2/FR-8).

Jinja + a single vendored stylesheet and a tiny vendored SSE client — no SPA, no
build step (D5). P1 rendered the run list and run detail; P2 made them *live* (a
small ``static/live.js`` opens an ``EventSource`` to ``/events`` and re-fetches
each ``[data-live-src]`` region on a transition); P5 added the gate/recovery
panel and the diff/judge-audit pages; P7 adds the durable auth surface (the
``/login`` token exchange + the session CSRF token surfaced via a ``<meta>`` tag)
and the full-history browser, cost report, and list search/sort/filter.

After P7 the page links carry **no token** — the browser authenticates by the
``HttpOnly`` login cookie (FR-10.4), so a bare ``/runs/<slug>`` link suffices and
the SSE handshake URL is token-free. State-changing fetches carry the
session-bound CSRF token from the ``<meta name="csrf-token">`` tag (FR-10.6).

(The PRD names HTMX for the live mechanism; it is realized as an equivalent
~30-line vendored vanilla SSE→fetch shim — DEV-1, ratified — with identical
behaviour and zero fetched dependency.)
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlencode

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from gauntlet.web.auth import COOKIE_NAME, SessionStore, safe_next
from gauntlet.web.gate import GateResolver, NoPendingGate
from gauntlet.web.intel import resume_intel
from gauntlet.web.markdown import render_markdown
from gauntlet.web.store import (
    RunNotFound,
    RunStore,
    UnsafePath,
    _safe_segment,
    duration_seconds,
)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def _lock_context(store: RunStore) -> dict:
    """The worktree-lock surface shared by every page (FR-10.5).

    ``locked`` disables Launch/Resume/Approve repo-wide; ``lock`` names the
    holder for the banner. The console only *surfaces* this — the enforcement is
    the engine lock.
    """
    lock = store.worktree_lock()
    return {"locked": lock is not None, "lock": lock}


def _detail_context(store: RunStore, slug: str, run_id: str | None) -> dict:
    """Shared context for the full detail page and its live partial."""
    man = store.manifest(slug, run_id)
    steps = [
        {
            "id": s.id,
            "type": s.type,
            "status": s.status,
            "agent": s.agent,
            "iteration": s.iteration,
            "notes": s.notes,
            "cost_usd": s.usage.cost_usd,
            "duration_s": duration_seconds(s.started, s.ended),
        }
        for s in man.steps
    ]
    lock = store.worktree_lock()
    owned, attached, external = store._ownership(slug, man.run_id, lock)
    # P5: the recovery classification drives the banner + which control forms to
    # offer (FR-5). Cheap (pure over the manifest). Gate evidence is resolved
    # only when parked at a gate/escalation (small JSON/artifact reads); the
    # heavier phase diff stays a deliberate navigation (the /runs/{slug}/diff
    # page), never recomputed on every live tick.
    intel = resume_intel(man)
    gate = None
    gate_error: str | None = None
    if intel.state in ("gate", "escalation"):
        try:
            gate = GateResolver(store).gate(slug, run_id)
        except (NoPendingGate, RunNotFound, UnsafePath, OSError) as exc:
            # Fail closed (review F-001): a gate/escalation whose evidence cannot
            # be safely assembled — an unsafe `show:` path, an unreadable
            # artifact, or a resolver/intel disagreement — must NOT silently
            # degrade into a normal decision page. Surface the failure and let
            # the template suppress the control forms, so no Approve/Reject/Resume
            # can be issued without the required evidence; the same fail-closed
            # posture as the /api/runs/{slug}/gate endpoint.
            gate = None
            gate_error = str(exc) or exc.__class__.__name__
    ctx = {
        "slug": slug,
        "manifest": man,
        "steps": steps,
        "owned": owned,
        "attached": attached,
        "external": external,
        "intel": intel,
        "gate": gate,
        "gate_error": gate_error,
    }
    ctx.update(_lock_context(store))
    return ctx


def _step_detail_context(
    store: RunStore,
    slug: str,
    step: str,
    run_id: str | None,
    iteration: str | None,
    artifact: str | None,
) -> dict:
    """Context for the step transcript drill-down page (FR-3.1).

    Resolves the step's on-disk artifacts via ``store.step_detail`` and merges
    in the manifest metadata for the matching record. A ``foreach`` fan-out
    stores several records under one ``id`` differing by ``iteration`` — pick the
    one named by ``?iteration=`` when given, else the first/only matching record.
    The selected ``?artifact=`` is read through the contained, allowlisted
    ``store.read_step_artifact`` (never an arbitrary path); it defaults to
    ``transcript.md`` when present so a step row lands on its transcript.
    """
    detail = store.step_detail(slug, step, run_id)
    man = store.manifest(slug, run_id)
    base = step.split(".")[0]
    records = [s for s in man.steps if s.id in (step, base)]
    record = None
    if iteration is not None:
        record = next(
            (s for s in records if (s.iteration or "") == iteration), None
        )
    if record is None:
        record = records[0] if records else None

    names = [a.name for a in detail.artifacts]
    # An explicit ?artifact= is validated through the contained, allowlisted
    # reader: an *unsafe* segment (traversal/separator/NUL) raises UnsafePath and
    # is surfaced as a 400 by the app's exception handler — never silently
    # downgraded to the default. A safe-but-absent name is shown as a content
    # error. With no ?artifact=, default to transcript.md when present.
    selected = artifact or (("transcript.md") if "transcript.md" in names else None)
    content: str | None = None
    content_error: str | None = None
    if selected is not None:
        if artifact is not None:
            _safe_segment(artifact, kind="artifact")
        try:
            content = store.read_step_artifact(
                slug, step, selected, run_id=run_id
            )
        except RunNotFound as exc:
            content = None
            content_error = str(exc) or exc.__class__.__name__

    started = record.started if record else None
    ended = record.ended if record else None
    return {
        "slug": slug,
        "step": step,
        "run_id": detail.run_id,
        "detail": detail,
        "record": record,
        "iteration": iteration,
        "duration_s": duration_seconds(started, ended),
        "artifact_names": names,
        "selected": selected,
        "content": content,
        "content_error": content_error,
    }


def register_views(
    app: FastAPI, store: RunStore, auth: Depends, sessions: SessionStore
) -> None:
    """Register the run-list/run-detail HTML routes, live partials, and /login."""
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    # FR-4.3: gate `show:` markdown artifacts render *as markdown* (safe, no dep).
    templates.env.filters["markdown"] = render_markdown

    def _csrf(request: Request) -> str:
        """The current session's CSRF token (empty for a header/no-cookie call).

        Surfaced into the page `<meta>` so the live-update fetch shim can attach
        it as ``X-CSRF-Token`` on cookie-authenticated POSTs (FR-10.6).
        """
        return sessions.session_csrf(request.cookies.get(COOKIE_NAME)) or ""

    # ---- /login token exchange (FR-10.4) — the only auth'd-page exception ----
    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str = Query(default="/")) -> HTMLResponse:
        # Unauthenticated (the login exchange itself). The token is pasted into a
        # POST form — never carried in a URL/query/history (FR-10.4). `next` is a
        # local path we return to after login.
        return templates.TemplateResponse(
            request, "login.html", {"next": safe_next(next), "error": None}
        )

    @app.post("/login")
    async def login(request: Request):
        # Parse the urlencoded form body with stdlib (no python-multipart dep, so
        # the M5 zero-new-deps budget holds). On a constant-time token match, mint
        # a fresh session + CSRF (rotated per login) and set the HttpOnly cookie.
        raw = (await request.body()).decode("utf-8", errors="replace")
        fields = parse_qs(raw, keep_blank_values=True)
        supplied = (fields.get("token") or [""])[0]
        nxt = safe_next((fields.get("next") or ["/"])[0])
        if not sessions.verify_token(supplied):
            return templates.TemplateResponse(
                request,
                "login.html",
                {"next": nxt, "error": "Invalid token."},
                status_code=401,
            )
        sid, _csrf_token = sessions.create_session()
        resp = RedirectResponse(nxt, status_code=303)
        # HttpOnly (no JS access) + SameSite=Strict (defence-in-depth on top of
        # the CSRF token) + host-only Path=/; Secure omitted for loopback http.
        resp.set_cookie(
            COOKIE_NAME, sid, httponly=True, samesite="strict", path="/"
        )
        return resp

    @app.post("/logout")
    def logout(request: Request, _auth=auth):
        # Drop the server-side session and clear the cookie. Guarded by `auth`
        # (so it carries CSRF like any cookie POST); idempotent.
        sessions.drop_session(request.cookies.get(COOKIE_NAME))
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp

    @app.get("/", response_class=HTMLResponse, dependencies=[auth])
    def run_list(
        request: Request,
        status: str | None = Query(default=None),
        slug: str | None = Query(default=None),
        q: str | None = Query(default=None),
        sort: str | None = Query(default=None),
    ) -> HTMLResponse:
        # FR-1.2: the same filters/sort as /api/runs, so the page and API agree.
        rows = store.list_rows(status=status, slug=slug, q=q, sort=sort)
        # Carry the active filter onto the live-partial fetch so an SSE swap keeps
        # the same view (live.js re-fetches /partials/runs<live_query>).
        params = {k: v for k, v in
                  (("status", status), ("slug", slug), ("q", q), ("sort", sort)) if v}
        live_query = ("?" + urlencode(params)) if params else ""
        ctx = {
            "rows": rows,
            "csrf_token": _csrf(request),
            "filter_status": status or "",
            "filter_q": q or "",
            "filter_sort": sort or "",
            "live_query": live_query,
        }
        ctx.update(_lock_context(store))
        return templates.TemplateResponse(request, "run_list.html", ctx)

    @app.get("/runs/{slug}", response_class=HTMLResponse, dependencies=[auth])
    def run_detail(
        request: Request, slug: str, run_id: str | None = Query(default=None)
    ) -> HTMLResponse:
        ctx = _detail_context(store, slug, run_id)
        ctx["csrf_token"] = _csrf(request)
        ctx["handoff_enabled"] = bool(getattr(app.state, "handoff_enabled", False))
        return templates.TemplateResponse(request, "run_detail.html", ctx)

    @app.get(
        "/runs/{slug}/steps/{step}",
        response_class=HTMLResponse,
        dependencies=[auth],
    )
    def step_detail_page(
        request: Request,
        slug: str,
        step: str,
        run_id: str | None = Query(default=None),
        iteration: str | None = Query(default=None),
        artifact: str | None = Query(default=None),
    ) -> HTMLResponse:
        # FR-3.1: the step transcript drill-down. Lists the step's artifacts and
        # renders the selected one (transcript.md as markdown by default); the
        # `?artifact=` choice is allowlisted in `store.read_step_artifact` so it
        # can never address an arbitrary path (containment, FR-10.1).
        ctx = _step_detail_context(store, slug, step, run_id, iteration, artifact)
        ctx["csrf_token"] = _csrf(request)
        return templates.TemplateResponse(request, "step_detail.html", ctx)

    # ---- full-history browser + cost report (P7, FR-2.4) --------------------
    @app.get("/runs/{slug}/history", response_class=HTMLResponse, dependencies=[auth])
    def history_page(request: Request, slug: str) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "history.html",
            {"slug": slug, "runs": store.run_history(slug), "csrf_token": _csrf(request)},
        )

    @app.get("/runs/{slug}/report", response_class=HTMLResponse, dependencies=[auth])
    def report_page(
        request: Request, slug: str, run_id: str | None = Query(default=None)
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "report.html",
            {
                "slug": slug,
                "report": store.report_text(slug, run_id=run_id),
                "csrf_token": _csrf(request),
            },
        )

    # ---- phase diff + judge-audit (P5, FR-4.3/FR-3.4) — deliberate pages, not
    # live-swapped (the diff shells out to git, so it is not on the SSE tick) ---
    @app.get("/runs/{slug}/diff", response_class=HTMLResponse, dependencies=[auth])
    def diff_page(
        request: Request,
        slug: str,
        run_id: str | None = Query(default=None),
        from_: str | None = Query(default=None, alias="from"),
        to: str | None = Query(default=None),
    ) -> HTMLResponse:
        view = GateResolver(store).diff(
            slug, run_id=run_id, from_sha=from_, to_sha=to
        )
        return templates.TemplateResponse(
            request,
            "diff.html",
            {"slug": slug, "diff": view, "csrf_token": _csrf(request)},
        )

    @app.get(
        "/runs/{slug}/judge-audit", response_class=HTMLResponse, dependencies=[auth]
    )
    def judge_audit_page(
        request: Request, slug: str, run_id: str | None = Query(default=None)
    ) -> HTMLResponse:
        entries = store.judge_audit(slug, run_id=run_id)
        return templates.TemplateResponse(
            request,
            "judge_audit.html",
            {"slug": slug, "entries": entries, "csrf_token": _csrf(request)},
        )

    # ---- live partials (P2): the innerHTML live.js swaps in on each SSE tick --
    @app.get("/partials/runs", response_class=HTMLResponse, dependencies=[auth])
    def partial_runs(
        request: Request,
        status: str | None = Query(default=None),
        slug: str | None = Query(default=None),
        q: str | None = Query(default=None),
        sort: str | None = Query(default=None),
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "_run_rows.html",
            {"rows": store.list_rows(status=status, slug=slug, q=q, sort=sort)},
        )

    @app.get(
        "/partials/runs/{slug}", response_class=HTMLResponse, dependencies=[auth]
    )
    def partial_run_detail(
        request: Request, slug: str, run_id: str | None = Query(default=None)
    ) -> HTMLResponse:
        ctx = _detail_context(store, slug, run_id)
        ctx["handoff_enabled"] = bool(getattr(app.state, "handoff_enabled", False))
        return templates.TemplateResponse(request, "_run_detail_body.html", ctx)


__all__ = ["register_views"]

"""Server-rendered console pages + live partials (P1/P2, FR-1/FR-2/FR-8).

Jinja + a single vendored stylesheet and a tiny vendored SSE client — no SPA, no
build step (D5). P1 rendered the run list and run detail statically; P2 makes
them *live*: a small ``static/live.js`` opens an ``EventSource`` to ``/events``
and, on each transition, re-fetches the page's live region from a **partial**
route (``/partials/runs`` for the list, ``/partials/runs/{slug}`` for the detail
body) and swaps its ``innerHTML``. The partials render the exact same fragments
the full pages embed, so a live swap and a fresh load are identical.

(The PRD names HTMX for this; it is realized here as an equivalent ~30-line
vendored vanilla SSE→fetch shim because the offline build sandbox cannot fetch
the htmx bytes and fabricating a minified library is not acceptable. The
functional outcome — declarative live partial swaps over SSE, no build step,
zero new heavy deps — is identical and, on the M5 dependency budget, strictly
leaner.)
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from gauntlet.web.gate import GateResolver, NoPendingGate
from gauntlet.web.intel import resume_intel
from gauntlet.web.markdown import render_markdown
from gauntlet.web.store import RunNotFound, RunStore, UnsafePath, duration_seconds

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def _lock_context(store: RunStore) -> dict:
    """The worktree-lock surface shared by every page (FR-10.5).

    ``locked`` disables Launch/Resume/Approve repo-wide; ``lock`` names the
    holder for the banner. The console only *surfaces* this — the enforcement is
    the engine lock.
    """
    lock = store.worktree_lock()
    return {"locked": lock is not None, "lock": lock}


def _detail_context(
    store: RunStore, slug: str, run_id: str | None, token: str | None
) -> dict:
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
        "token": token or "",
        "owned": owned,
        "attached": attached,
        "external": external,
        "intel": intel,
        "gate": gate,
        "gate_error": gate_error,
    }
    ctx.update(_lock_context(store))
    return ctx


def register_views(app: FastAPI, store: RunStore, auth: Depends) -> None:
    """Register the run-list/run-detail HTML routes and their live partials."""
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    # FR-4.3: gate `show:` markdown artifacts render *as markdown* (safe, no dep).
    templates.env.filters["markdown"] = render_markdown

    @app.get("/", response_class=HTMLResponse, dependencies=[auth])
    def run_list(
        request: Request, token: str | None = Query(default=None)
    ) -> HTMLResponse:
        ctx = {"rows": store.list_rows(), "token": token or ""}
        ctx.update(_lock_context(store))
        return templates.TemplateResponse(request, "run_list.html", ctx)

    @app.get("/runs/{slug}", response_class=HTMLResponse, dependencies=[auth])
    def run_detail(
        request: Request,
        slug: str,
        run_id: str | None = Query(default=None),
        token: str | None = Query(default=None),
    ) -> HTMLResponse:
        ctx = _detail_context(store, slug, run_id, token)
        return templates.TemplateResponse(request, "run_detail.html", ctx)

    # ---- phase diff + judge-audit (P5, FR-4.3/FR-3.4) — deliberate pages, not
    # live-swapped (the diff shells out to git, so it is not on the SSE tick) ---
    @app.get("/runs/{slug}/diff", response_class=HTMLResponse, dependencies=[auth])
    def diff_page(
        request: Request,
        slug: str,
        run_id: str | None = Query(default=None),
        from_: str | None = Query(default=None, alias="from"),
        to: str | None = Query(default=None),
        token: str | None = Query(default=None),
    ) -> HTMLResponse:
        view = GateResolver(store).diff(
            slug, run_id=run_id, from_sha=from_, to_sha=to
        )
        return templates.TemplateResponse(
            request, "diff.html", {"slug": slug, "diff": view, "token": token or ""}
        )

    @app.get(
        "/runs/{slug}/judge-audit", response_class=HTMLResponse, dependencies=[auth]
    )
    def judge_audit_page(
        request: Request,
        slug: str,
        run_id: str | None = Query(default=None),
        token: str | None = Query(default=None),
    ) -> HTMLResponse:
        entries = store.judge_audit(slug, run_id=run_id)
        return templates.TemplateResponse(
            request,
            "judge_audit.html",
            {"slug": slug, "entries": entries, "token": token or ""},
        )

    # ---- live partials (P2): the innerHTML live.js swaps in on each SSE tick --
    @app.get("/partials/runs", response_class=HTMLResponse, dependencies=[auth])
    def partial_runs(
        request: Request, token: str | None = Query(default=None)
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "_run_rows.html",
            {"rows": store.list_rows(), "token": token or ""},
        )

    @app.get(
        "/partials/runs/{slug}", response_class=HTMLResponse, dependencies=[auth]
    )
    def partial_run_detail(
        request: Request,
        slug: str,
        run_id: str | None = Query(default=None),
        token: str | None = Query(default=None),
    ) -> HTMLResponse:
        ctx = _detail_context(store, slug, run_id, token)
        return templates.TemplateResponse(request, "_run_detail_body.html", ctx)


__all__ = ["register_views"]

"""Server-rendered console pages (P1, FR-1/FR-2).

Jinja + a single vendored CSS file — no SPA, no build step (D5). P1 renders the
run list and run detail statically; HTMX-driven live updates land in P2. The
page routes are registered onto the app by :func:`register_views` so
``service.py`` stays the thin app factory.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from gauntlet.web.store import RunStore, duration_seconds

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def register_views(app: FastAPI, store: RunStore, auth: Depends) -> None:
    """Register the run-list and run-detail HTML routes onto ``app``."""
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    @app.get("/", response_class=HTMLResponse, dependencies=[auth])
    def run_list(
        request: Request, token: str | None = Query(default=None)
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "run_list.html",
            {"rows": store.list_rows(), "token": token or ""},
        )

    @app.get("/runs/{slug}", response_class=HTMLResponse, dependencies=[auth])
    def run_detail(
        request: Request,
        slug: str,
        run_id: str | None = Query(default=None),
        token: str | None = Query(default=None),
    ) -> HTMLResponse:
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
        return templates.TemplateResponse(
            request,
            "run_detail.html",
            {"slug": slug, "manifest": man, "steps": steps, "token": token or ""},
        )


__all__ = ["register_views"]

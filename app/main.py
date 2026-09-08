"""Beacon ASGI application factory.

Milestone 0 scope: app instance boots, exposes a health-check route, wires
up settings/i18n scaffolding, and mounts the auth/agent-account/audit-log
routes added for the "Foundations" auth & audit work. Milestone 1 adds the
Event/EventConfig/Show/TicketType backoffice-core routes. Milestone 2 adds
the public read/checkout routes (``app.api.routes.public``) and the
``/sitemap.xml``/``/robots.txt`` SEO routes (``app.api.routes.seo``).
"""

from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from starlette.responses import RedirectResponse
from starlette.staticfiles import StaticFiles

from app.api.routes import (
    agent_accounts,
    audit_log,
    auth,
    email_templates,
    event_configs,
    events,
    orders,
    public,
    seo,
    shows,
    themes,
    ticket_types,
)
from app.core.config import get_settings
from app.web.deps import WebAuthRequired
from app.web.routes import auth as web_auth
from app.web.routes import email_templates as web_email_templates
from app.web.routes import events as web_events
from app.web.routes import orders as web_orders
from app.web.routes import public_site as web_public_site
from app.web.routes import themes as web_themes


def create_app() -> FastAPI:
    """Build and configure the FastAPI application instance."""
    settings = get_settings()

    app = FastAPI(
        title="Beacon",
        description="Self-hosted, generic event ticketing platform.",
        version="0.1.0",
    )

    app.include_router(auth.router)
    app.include_router(agent_accounts.router)
    app.include_router(audit_log.router)
    app.include_router(events.router)
    app.include_router(event_configs.router)
    app.include_router(shows.router)
    app.include_router(ticket_types.router)
    app.include_router(themes.router)
    app.include_router(email_templates.router)
    app.include_router(orders.router)
    app.include_router(orders.list_router)
    app.include_router(public.router)
    app.include_router(seo.router)

    # Server-rendered backoffice HTML pages (Jinja2 + HTMX), added in
    # Milestone 1.5 by `frontend-theming` — see app/web/. Distinct from the
    # JSON API routers above: these render templates and proxy in-process
    # to the JSON API (app.web.api_client) rather than duplicating its
    # business logic.
    app.include_router(web_auth.router)
    app.include_router(web_events.router)
    app.include_router(web_themes.router)
    app.include_router(web_email_templates.router)
    app.include_router(web_orders.router)

    # Public-site HTML pages (Milestone 2, `frontend-theming`): themed
    # landing/preview pages, checkout form, order confirmation — see
    # app/web/routes/public_site.py. Registered after the backoffice web
    # routers so a path clash (there isn't one today) would favor the
    # backoffice; kept as its own router since these pages are
    # unauthenticated and use a separate themed Jinja environment
    # (app.core.public_templating), not the backoffice one.
    app.include_router(web_public_site.router)

    @app.exception_handler(WebAuthRequired)
    async def _redirect_to_login(request: Request, exc: WebAuthRequired) -> RedirectResponse:
        """Backoffice pages redirect an unauthenticated visitor to the login
        form instead of returning a JSON 401 (see app.web.deps)."""
        return RedirectResponse(url=f"/login?next={quote(exc.next_path)}", status_code=303)

    # Serves uploaded Theme logo/background images back out (see
    # app.services.theme_images) — local filesystem storage, mounted as its
    # own docker volume in docker-compose.yml so it survives rebuilds.
    # Created eagerly here (not lazily on first upload) since StaticFiles
    # requires the directory to exist at mount time.
    uploads_dir = Path(settings.uploads_dir)
    uploads_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/uploads", StaticFiles(directory=str(uploads_dir)), name="uploads")

    # Backoffice static assets (CSS, vendored htmx.min.js — see app/static/).
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        """Liveness/readiness probe. Returns 200 once the app has booted.

        Deliberately does not check DB connectivity: compose/orchestrator
        health checks should be able to distinguish "app process is up"
        from "DB is reachable" rather than conflating the two.
        """
        return {"status": "ok", "env": settings.app_env}

    return app


app = create_app()

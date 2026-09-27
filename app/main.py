"""Beacon ASGI application factory: mounts the JSON API, the HTML routers, static files and uploads."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from starlette.responses import RedirectResponse
from starlette.staticfiles import StaticFiles

from app.api.routes import (
    admin_users,
    agent_accounts,
    audit_log,
    auth,
    email_templates,
    event_configs,
    events,
    orders,
    public,
    scan,
    scan_shows,
    seo,
    shows,
    stats,
    themes,
    ticket_types,
)
from app.core.config import get_settings
from app.services.order_expiry import run_order_expiry_background_loop
from app.web.deps import WebAuthRequired
from app.web.routes import admin_users as web_admin_users
from app.web.routes import agent_accounts as web_agent_accounts
from app.web.routes import audit_log as web_audit_log
from app.web.routes import auth as web_auth
from app.web.routes import demo_payment as web_demo_payment
from app.web.routes import email_templates as web_email_templates
from app.web.routes import event_config as web_event_config
from app.web.routes import events as web_events
from app.web.routes import homepage as web_homepage
from app.web.routes import orders as web_orders
from app.web.routes import public_site as web_public_site
from app.web.routes import scan as web_scan
from app.web.routes import shows as web_shows
from app.web.routes import stats as web_stats
from app.web.routes import themes as web_themes


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the stale-order expiry loop alongside the app. On shutdown, signal it
    and await it rather than cancelling, so a sweep is never torn down
    mid-transaction.
    """
    stop_event = asyncio.Event()
    task = asyncio.create_task(run_order_expiry_background_loop(stop_event=stop_event))
    try:
        yield
    finally:
        stop_event.set()
        await task


def create_app() -> FastAPI:
    """Build and configure the FastAPI application instance."""
    settings = get_settings()

    app = FastAPI(
        title="Beacon",
        description="Self-hosted, generic event ticketing platform.",
        version="0.1.0",
        lifespan=_lifespan,
    )

    app.include_router(auth.router)
    app.include_router(admin_users.router)
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
    app.include_router(orders.show_router)
    app.include_router(public.router)
    app.include_router(scan.router)
    app.include_router(scan_shows.router)
    app.include_router(seo.router)
    app.include_router(stats.router)

    # Backoffice HTML pages (they call the JSON API in-process).
    app.include_router(web_auth.router)
    app.include_router(web_events.router)
    app.include_router(web_event_config.router)
    app.include_router(web_themes.router)
    app.include_router(web_email_templates.router)
    app.include_router(web_orders.router)
    app.include_router(web_scan.router)
    app.include_router(web_stats.router)
    app.include_router(web_admin_users.router)
    app.include_router(web_agent_accounts.router)
    app.include_router(web_audit_log.router)
    app.include_router(web_shows.router)

    # Public pages: unauthenticated, with their own themed Jinja environment.
    # Registered after the backoffice so a path clash would favor the backoffice.
    app.include_router(web_public_site.router)

    # Demo payment provider pages.
    app.include_router(web_demo_payment.router)

    # The public homepage at `/`.
    app.include_router(web_homepage.router)

    @app.exception_handler(WebAuthRequired)
    async def _redirect_to_login(request: Request, exc: WebAuthRequired) -> RedirectResponse:
        """Unauthenticated backoffice pages redirect to login instead of a JSON 401."""
        return RedirectResponse(url=f"/login?next={quote(exc.next_path)}", status_code=303)

    # Uploaded theme images (a docker volume). Created eagerly: StaticFiles
    # requires the directory to exist at mount time.
    uploads_dir = Path(settings.uploads_dir)
    uploads_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/uploads", StaticFiles(directory=str(uploads_dir)), name="uploads")

    # Backoffice static assets (CSS, vendored htmx.min.js — see app/static/).
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        """Liveness probe. Doesn't check the DB, so "app is up" and "DB is reachable"
        stay distinguishable.
        """
        return {"status": "ok", "env": settings.app_env}

    return app


app = create_app()

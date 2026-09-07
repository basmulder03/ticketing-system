"""Beacon ASGI application factory.

Milestone 0 scope: app instance boots, exposes a health-check route, wires
up settings/i18n scaffolding, and mounts the auth/agent-account/audit-log
routes added for the "Foundations" auth & audit work. Milestone 1 adds the
Event/EventConfig/Show/TicketType backoffice-core routes. Public-site/
checkout routes are added by ``backend-builder`` in subsequent milestones.
"""

from pathlib import Path

from fastapi import FastAPI
from starlette.staticfiles import StaticFiles

from app.api.routes import (
    agent_accounts,
    audit_log,
    auth,
    event_configs,
    events,
    shows,
    themes,
    ticket_types,
)
from app.core.config import get_settings


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

    # Serves uploaded Theme logo/background images back out (see
    # app.services.theme_images) — local filesystem storage, mounted as its
    # own docker volume in docker-compose.yml so it survives rebuilds.
    # Created eagerly here (not lazily on first upload) since StaticFiles
    # requires the directory to exist at mount time.
    uploads_dir = Path(settings.uploads_dir)
    uploads_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/uploads", StaticFiles(directory=str(uploads_dir)), name="uploads")

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

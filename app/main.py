"""Beacon ASGI application factory.

Milestone 0 scope only: app instance boots, exposes a health-check route,
and wires up settings/i18n scaffolding. Routes/models/business logic are
added by ``backend-builder`` in subsequent milestones.
"""

from fastapi import FastAPI

from app.core.config import get_settings


def create_app() -> FastAPI:
    """Build and configure the FastAPI application instance."""
    settings = get_settings()

    app = FastAPI(
        title="Beacon",
        description="Self-hosted, generic event ticketing platform.",
        version="0.1.0",
    )

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

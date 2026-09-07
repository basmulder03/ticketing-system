"""Seed the local dev database with demo data.

Milestone 0 scope: seeds a single demo ``AdminUser`` (admin role) from the
``SEED_ADMIN_EMAIL``/``SEED_ADMIN_PASSWORD`` settings, so the auth flow can
be exercised immediately after ``docker-compose up`` without a manual
signup step (no signup route exists — admin accounts are provisioned out
of band). Idempotent: safe to re-run against an already-seeded DB (upserts
by email), so ``./scripts/dev-reseed.sh`` works.

Event/Show/TicketType/EventConfig demo data is added by ``backend-builder``
starting Milestone 1, once those models exist.

Run via: `docker-compose exec app python scripts/seed.py`
(also wired into `./scripts/dev-up.sh` / `./scripts/dev-reseed.sh`).
"""

import asyncio

from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import async_session_factory
from app.models.admin_user import AdminUser
from app.models.enums import AdminRole


async def seed() -> None:
    """Populate demo data: currently just the one local-dev AdminUser."""
    settings = get_settings()
    async with async_session_factory() as session:
        result = await session.execute(
            select(AdminUser).where(AdminUser.email == settings.seed_admin_email.lower())
        )
        admin = result.scalar_one_or_none()
        if admin is not None:
            print(f"[seed] AdminUser {settings.seed_admin_email!r} already exists, skipping.")
            return

        admin = AdminUser(
            email=settings.seed_admin_email.lower(),
            hashed_password=hash_password(settings.seed_admin_password),
            role=AdminRole.ADMIN,
            is_active=True,
        )
        session.add(admin)
        await session.commit()
        print(
            f"[seed] Created demo AdminUser {settings.seed_admin_email!r} "
            f"(password from SEED_ADMIN_PASSWORD — local dev only, never use in production)."
        )


if __name__ == "__main__":
    asyncio.run(seed())

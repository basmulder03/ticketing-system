"""Seed the local dev database with demo data.

Milestone 0 scope: seeds a single demo ``AdminUser`` (admin role) from the
``SEED_ADMIN_EMAIL``/``SEED_ADMIN_PASSWORD`` settings, so the auth flow can
be exercised immediately after ``docker-compose up`` without a manual
signup step (no signup route exists — admin accounts are provisioned out
of band).

Milestone 1 scope: seeds one demo Event (with its EventConfig pointed at
the local Mailpit SMTP sink and the placeholder Mollie test key, both from
``SEED_*`` settings — see ``app.core.config.Settings``), one Show under it,
and one TicketType under that Show, so there's something to click through /
exercise via the API immediately, per PROJECT_BRIEF.md's Developer
Experience requirement ("seeds at least one demo Event/Show/TicketType").

Idempotent throughout: safe to re-run against an already-seeded DB (upserts
by natural key — email for AdminUser, slug for Event), so
``./scripts/dev-reseed.sh`` works.

Run via: `docker-compose exec app python scripts/seed.py`
(also wired into `./scripts/dev-up.sh` / `./scripts/dev-reseed.sh`).
"""

import asyncio
import datetime as dt
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import async_session_factory
from app.models.admin_user import AdminUser
from app.models.enums import AdminRole, PaymentMethod, PublishStatus, SmtpEncryptionMode
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.show import Show
from app.models.ticket_type import TicketType

DEMO_EVENT_SLUG = "christmas-passion-demo"


async def _seed_admin_user(session: AsyncSession) -> None:
    """Create the demo local-dev AdminUser, unless it already exists."""
    settings = get_settings()
    result = await session.execute(select(AdminUser).where(AdminUser.email == settings.seed_admin_email.lower()))
    if result.scalar_one_or_none() is not None:
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


async def _seed_demo_event(session: AsyncSession) -> None:
    """Create one demo Event + EventConfig + Show + TicketType, unless the
    Event already exists (looked up by its fixed demo slug)."""
    result = await session.execute(select(Event).where(Event.slug == DEMO_EVENT_SLUG))
    if result.scalar_one_or_none() is not None:
        print(f"[seed] Event {DEMO_EVENT_SLUG!r} already exists, skipping.")
        return

    settings = get_settings()

    event = Event(
        name="Christmas Passion (Demo)",
        slug=DEMO_EVENT_SLUG,
        description="Demo event seeded for local development — exercise the full backoffice flow against it.",
        status=PublishStatus.DRAFT,
        sales_paused=False,
    )
    session.add(event)
    await session.flush()

    config = EventConfig(
        event_id=event.id,
        smtp_host=settings.seed_smtp_host,
        smtp_port=settings.seed_smtp_port,
        smtp_encryption=SmtpEncryptionMode.STARTTLS if settings.seed_smtp_use_tls else SmtpEncryptionMode.NONE,
        smtp_username=settings.seed_smtp_username or None,
        smtp_password=settings.seed_smtp_password or None,
        sender_name=settings.seed_smtp_sender_name,
        sender_email=settings.seed_smtp_sender_email,
        mollie_test_api_key=settings.seed_mollie_test_api_key,
        invoice_company_name="Het Kruispunt Landsmeer (Demo)",
        invoice_number_prefix="DEMO-",
        enabled_payment_methods=[PaymentMethod.MOLLIE, PaymentMethod.DOOR],
    )
    session.add(config)

    show = Show(
        event_id=event.id,
        date=dt.datetime.now(tz=dt.UTC).date() + dt.timedelta(days=90),
        doors_time=dt.time(19, 30),
        start_time=dt.time(20, 0),
        venue_name="Demo Venue Hall",
        venue_address="Voorbeeldstraat 1, 1121 AA Landsmeer",
        capacity=200,
        status=PublishStatus.DRAFT,
    )
    session.add(show)
    await session.flush()

    ticket_type = TicketType(
        show_id=show.id,
        name="Adult",
        price=Decimal("15.00"),
        service_fee_included=True,
        quantity_available=150,
    )
    session.add(ticket_type)

    await session.commit()
    print(f"[seed] Created demo Event {DEMO_EVENT_SLUG!r} with EventConfig, one Show, and one TicketType.")


async def seed() -> None:
    """Populate all demo data: AdminUser, then Event/EventConfig/Show/TicketType."""
    async with async_session_factory() as session:
        await _seed_admin_user(session)
        await _seed_demo_event(session)


if __name__ == "__main__":
    asyncio.run(seed())

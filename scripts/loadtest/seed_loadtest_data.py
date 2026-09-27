"""Seed (or inspect) a published event with a small, fresh ticket type for load
testing (see ``scripts/loadtest/README.md``).

Separate from ``scripts/seed.py`` because a load test needs published data,
a chosen small quantity (to force contention), and a fresh ticket type per
run (a depleted one only measures 409s).

    docker-compose exec app python scripts/loadtest/seed_loadtest_data.py seed --quantity 100
    docker-compose exec app python scripts/loadtest/seed_loadtest_data.py status --ticket-type-id <uuid>

or natively:

    SEED_SMTP_HOST=localhost .venv/bin/python scripts/loadtest/seed_loadtest_data.py seed --quantity 100
"""

import argparse
import asyncio
import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import async_session_factory
from app.models.enums import PaymentMethod, PublishStatus, SmtpEncryptionMode
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.show import Show
from app.models.ticket_type import TicketType
from app.services.stock import sold_counts_for_ticket_types

LOADTEST_EVENT_SLUG = "loadtest"
"""The reusable load-test event (shows/ticket types are fresh each run)."""


async def _get_or_create_event(session: AsyncSession) -> Event:
    """The load-test event, created published with ``door`` payments if missing."""
    result = await session.execute(select(Event).where(Event.slug == LOADTEST_EVENT_SLUG))
    event = result.scalar_one_or_none()
    if event is not None:
        return event

    settings = get_settings()
    event = Event(
        name="Load Test Event (do not use for real sales)",
        slug=LOADTEST_EVENT_SLUG,
        description="Dedicated Event for scripts/loadtest/ — Milestone 9 sales-live capacity testing.",
        status=PublishStatus.PUBLISHED,
        sales_paused=False,
    )
    session.add(event)
    await session.flush()

    # Door only: every method goes through the same stock lock, and door keeps
    # Mollie's latency out of the measurement.
    config = EventConfig(
        event_id=event.id,
        smtp_host=settings.seed_smtp_host,
        smtp_port=settings.seed_smtp_port,
        smtp_encryption=SmtpEncryptionMode.STARTTLS if settings.seed_smtp_use_tls else SmtpEncryptionMode.NONE,
        smtp_username=settings.seed_smtp_username or None,
        smtp_password=settings.seed_smtp_password or None,
        sender_name="Beacon Load Test",
        sender_email=settings.seed_smtp_sender_email,
        enabled_payment_methods=[PaymentMethod.DOOR],
    )
    session.add(config)
    await session.commit()
    print(f"[loadtest-seed] Created Event {LOADTEST_EVENT_SLUG!r} (PUBLISHED) with a door-only EventConfig.")
    return event


async def _create_show_and_ticket_type(session: AsyncSession, event: Event, quantity: int) -> TicketType:
    """A fresh published show with one ticket type of ``quantity``."""
    run_label = dt.datetime.now(tz=dt.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    show = Show(
        event_id=event.id,
        date=dt.datetime.now(tz=dt.UTC).date() + dt.timedelta(days=1),
        doors_time=dt.time(19, 0),
        start_time=dt.time(20, 0),
        venue_name="Load Test Venue",
        venue_address="N/A",
        capacity=max(quantity, 1),
        status=PublishStatus.PUBLISHED,
    )
    session.add(show)
    await session.flush()

    ticket_type = TicketType(
        show_id=show.id,
        name=f"Load test run {run_label}",
        price=Decimal("10.00"),
        service_fee_included=True,
        quantity_available=quantity,
    )
    session.add(ticket_type)
    await session.commit()
    return ticket_type


async def seed(quantity: int) -> None:
    """Seed a fresh scarce ticket type and print what ``locustfile.py`` needs."""
    async with async_session_factory() as session:
        event = await _get_or_create_event(session)
        ticket_type = await _create_show_and_ticket_type(session, event, quantity)

    print("[loadtest-seed] Ready. Run the load test against this TicketType:")
    print(f"[loadtest-seed]   TICKET_TYPE_ID={ticket_type.id}")
    print(f"[loadtest-seed]   quantity_available={quantity}")
    print(
        "[loadtest-seed] Example:\n"
        f"    TICKET_TYPE_ID={ticket_type.id} \\\n"
        "    locust -f scripts/loadtest/locustfile.py --host=http://localhost:8000 \\\n"
        f"      --headless --users {quantity * 3} --spawn-rate {quantity * 3} --run-time 30s"
    )


async def status(ticket_type_id: uuid.UUID) -> None:
    """Print live remaining stock — the "did it oversell?" check after a run."""
    async with async_session_factory() as session:
        result = await session.execute(select(TicketType).where(TicketType.id == ticket_type_id))
        ticket_type = result.scalar_one_or_none()
        if ticket_type is None:
            print(f"[loadtest-status] No TicketType with id {ticket_type_id} found.")
            return
        sold_counts = await sold_counts_for_ticket_types(session, [ticket_type_id])
        sold = sold_counts.get(ticket_type_id, 0)
        remaining = ticket_type.quantity_available - sold

    print(f"[loadtest-status] TicketType {ticket_type_id} ({ticket_type.name!r}):")
    print(f"[loadtest-status]   quantity_available = {ticket_type.quantity_available}")
    print(f"[loadtest-status]   sold (live tickets) = {sold}")
    print(f"[loadtest-status]   remaining           = {remaining}")
    if sold > ticket_type.quantity_available:
        print(
            f"[loadtest-status]   *** OVERSOLD by {sold - ticket_type.quantity_available} — "
            "this must never happen; treat as a critical bug, not a load-test artifact. ***"
        )
    else:
        print("[loadtest-status]   OK — sold count does not exceed quantity_available.")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    seed_parser = subparsers.add_parser("seed", help="Create a fresh scarce TicketType for a load-test run.")
    seed_parser.add_argument(
        "--quantity",
        type=int,
        default=100,
        help="TicketType.quantity_available to seed (default: 100 — deliberately small so a realistic "
        "virtual-user count guarantees contention).",
    )

    status_parser = subparsers.add_parser(
        "status", help="Print live sold/remaining stock for a TicketType (post-load-test verification)."
    )
    status_parser.add_argument("--ticket-type-id", type=uuid.UUID, required=True)

    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.command == "seed":
        asyncio.run(seed(args.quantity))
    elif args.command == "status":
        asyncio.run(status(args.ticket_type_id))


if __name__ == "__main__":
    main()

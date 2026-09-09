"""Integration tests for the Milestone 8 Stats & Reporting routes:
``GET /api/v1/events/{event_id}/stats`` (``app.api.routes.stats.get_stats``)
and ``GET /api/v1/events/{event_id}/orders/export.csv``
(``app.api.routes.stats.export_orders_csv``).

Admin-only scoping (agent/scanner/unauthenticated all rejected) is covered
by the shared table in ``tests/integration/test_deps_admin_scoping.py`` —
not repeated here (both routes are registered there).

This file covers the routes' own behavior, in particular the two
deliberate design decisions documented in ``app.services.stats``' module
docstring:

1. ``EventStatsOut``'s per-``TicketType`` ``sold``/``revenue`` figures use
   the same "live" counting rule as
   ``app.services.stock.sold_counts_for_ticket_types`` (every Ticket on a
   non-cancelled/non-expired Order, INCLUDING pending/pending_door), while
   ``revenue_total``/``revenue_by_payment_method`` count ONLY ``paid``
   orders — these two populations are deliberately NOT meant to reconcile
   while an Event has pending orders in flight. See
   ``test_mixed_order_statuses_produce_two_deliberately_different_sold_and_revenue_populations``.
2. ``revenue_by_payment_method`` always has exactly one ``mollie`` and one
   ``door`` entry, zero-filled when a method has no paid orders.

Rows are built directly against the DB (mirrors
``tests/integration/test_stock_service.py``'s "insert Order/Ticket rows
directly" convention) rather than driving the full HTTP checkout/Mollie
flow, since these tests care about aggregation over a fixed, known set of
statuses rather than the checkout flow itself.
"""

import csv
import io
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import OrderStatus, PaymentMethod, PublishStatus
from app.models.event import Event
from app.models.order import Order
from app.models.show import Show
from app.models.ticket import Ticket
from app.models.ticket_type import TicketType
from tests.integration.conftest import SeededAdmin

_PAST = datetime.now(UTC) - timedelta(days=1)


# --- Shared setup helpers ----------------------------------------------------


async def _login_admin(client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]) -> None:
    seeded = await make_admin_user()
    login = await client.post(
        "/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password}
    )
    assert login.status_code == 200


async def _make_order_with_tickets(
    session: AsyncSession,
    *,
    event_id: uuid.UUID,
    ticket_type_id: uuid.UUID,
    status: OrderStatus,
    payment_method: PaymentMethod = PaymentMethod.DOOR,
    quantity: int = 1,
    total: Decimal = Decimal("10.00"),
    mollie_payment_id: str | None = None,
    scanned_quantity: int = 0,
) -> Order:
    """Insert an Order with ``quantity`` Ticket rows directly, bypassing
    checkout — mirrors ``test_stock_service.py``'s helper of the same name.
    The first ``scanned_quantity`` tickets get a real ``scanned_at``."""
    order = Order(
        event_id=event_id,
        buyer_name="Buyer",
        buyer_email=f"buyer-{uuid.uuid4().hex}@example.test",
        buyer_address="1 Test Street",
        status=status,
        payment_method=payment_method,
        mollie_payment_id=mollie_payment_id,
        total=total,
        language="en",
    )
    session.add(order)
    await session.flush()
    for index in range(quantity):
        ticket = Ticket(order_id=order.id, ticket_type_id=ticket_type_id)
        if index < scanned_quantity:
            ticket.scanned_at = datetime.now(UTC)
        session.add(ticket)
    await session.commit()
    await session.refresh(order)
    return order


# --- GET /stats: 404 / empty event -----------------------------------------


async def test_stats_returns_404_for_malformed_event_id(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login_admin(client, make_admin_user)

    response = await client.get("/api/v1/events/not-a-uuid/stats")

    assert response.status_code == 404


async def test_stats_returns_404_for_unknown_event_id(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login_admin(client, make_admin_user)

    response = await client.get(f"/api/v1/events/{uuid.uuid4()}/stats")

    assert response.status_code == 404


async def test_stats_for_event_with_no_shows_is_empty_not_an_error(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
) -> None:
    await _login_admin(client, make_admin_user)
    event = await make_event()

    response = await client.get(f"/api/v1/events/{event.id}/stats")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["shows"] == []
    assert body["sold_total"] == 0
    assert body["scanned_total"] == 0
    assert body["revenue_total"] == "0.00"
    methods = {entry["payment_method"] for entry in body["revenue_by_payment_method"]}
    assert methods == {"mollie", "door"}
    for entry in body["revenue_by_payment_method"]:
        assert entry["revenue"] == "0.00"
        assert entry["order_count"] == 0


# --- The most important test: two deliberately different populations -------


async def test_mixed_order_statuses_produce_two_deliberately_different_sold_and_revenue_populations(
    client: AsyncClient,
    db_session: AsyncSession,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
) -> None:
    """The single most important test in this file — see module docstring
    point 1. Builds a genuine mix of order statuses (paid, pending,
    pending_door, cancelled, expired) across two TicketTypes and asserts:

    - ``sold``/``scanned`` per-TicketType figures use the "live" rule
      (everything except cancelled/expired), i.e. INCLUDE pending/
      pending_door.
    - ``revenue_total``/``revenue_by_payment_method`` count ONLY paid
      orders.
    - The two populations genuinely differ (sold_total > paid order count),
      proving they are not silently the same query.
    """
    await _login_admin(client, make_admin_user)
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    adult = await make_ticket_type(show_id=show.id, name="Adult", price=Decimal("20.00"), quantity_available=50)
    child = await make_ticket_type(show_id=show.id, name="Child", price=Decimal("10.00"), quantity_available=50)

    # Adult: one paid (2 tickets, scanned), one pending (1 ticket), one
    # cancelled (3 tickets - excluded entirely).
    await _make_order_with_tickets(
        db_session,
        event_id=event.id,
        ticket_type_id=adult.id,
        status=OrderStatus.PAID,
        payment_method=PaymentMethod.MOLLIE,
        quantity=2,
        total=Decimal("40.00"),
        mollie_payment_id="tr_adult_paid",
        scanned_quantity=1,
    )
    await _make_order_with_tickets(
        db_session,
        event_id=event.id,
        ticket_type_id=adult.id,
        status=OrderStatus.PENDING,
        payment_method=PaymentMethod.MOLLIE,
        quantity=1,
        total=Decimal("20.00"),
    )
    await _make_order_with_tickets(
        db_session,
        event_id=event.id,
        ticket_type_id=adult.id,
        status=OrderStatus.CANCELLED,
        payment_method=PaymentMethod.MOLLIE,
        quantity=3,
        total=Decimal("60.00"),
    )

    # Child: one pending_door (1 ticket), one expired (4 tickets -
    # excluded).
    await _make_order_with_tickets(
        db_session,
        event_id=event.id,
        ticket_type_id=child.id,
        status=OrderStatus.PENDING_DOOR,
        payment_method=PaymentMethod.DOOR,
        quantity=1,
        total=Decimal("10.00"),
    )
    await _make_order_with_tickets(
        db_session,
        event_id=event.id,
        ticket_type_id=child.id,
        status=OrderStatus.EXPIRED,
        payment_method=PaymentMethod.DOOR,
        quantity=4,
        total=Decimal("40.00"),
    )
    # A paid door order for Child too, so both payment methods have
    # non-zero paid revenue.
    await _make_order_with_tickets(
        db_session,
        event_id=event.id,
        ticket_type_id=child.id,
        status=OrderStatus.PAID,
        payment_method=PaymentMethod.DOOR,
        quantity=1,
        total=Decimal("10.00"),
    )

    response = await client.get(f"/api/v1/events/{event.id}/stats")

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["shows"]) == 1
    ticket_type_stats = {tt["name"]: tt for tt in body["shows"][0]["ticket_types"]}

    # "Live" sold rule: paid(2) + pending(1) = 3 for Adult, INCLUDES
    # pending; cancelled(3) excluded.
    assert ticket_type_stats["Adult"]["sold"] == 3
    assert ticket_type_stats["Adult"]["revenue"] == "60.00"  # 3 * 20.00, unfiltered by paid status
    assert ticket_type_stats["Adult"]["scanned"] == 1

    # pending_door(1) counted, expired(4) excluded, paid(1) counted -> 2.
    assert ticket_type_stats["Child"]["sold"] == 2
    assert ticket_type_stats["Child"]["revenue"] == "20.00"  # 2 * 10.00
    assert ticket_type_stats["Child"]["scanned"] == 0

    assert body["sold_total"] == 5
    assert body["scanned_total"] == 1

    # revenue_total/revenue_by_payment_method: ONLY paid orders (Adult
    # paid=40.00 mollie, Child paid=10.00 door) = 50.00 total. This is a
    # DIFFERENT population from sold/revenue above (60.00 + 20.00 = 80.00
    # "live" ticket-type revenue) — the divergence is intentional, not a
    # bug (see app.services.stats module docstring).
    assert body["revenue_total"] == "50.00"
    revenue_by_method = {entry["payment_method"]: entry for entry in body["revenue_by_payment_method"]}
    assert revenue_by_method["mollie"]["revenue"] == "40.00"
    assert revenue_by_method["mollie"]["order_count"] == 1
    assert revenue_by_method["door"]["revenue"] == "10.00"
    assert revenue_by_method["door"]["order_count"] == 1

    # The two populations must genuinely differ, proving this isn't
    # accidentally the same query: sum of ticket-type "revenue" (80.00)
    # does not equal revenue_total (50.00).
    ticket_type_revenue_sum = sum(Decimal(tt["revenue"]) for tt in ticket_type_stats.values())
    assert ticket_type_revenue_sum != Decimal(body["revenue_total"])


# --- scanned counts: excluded for cancelled/expired orders -----------------


async def test_scanned_ticket_on_a_cancelled_order_is_not_counted(
    client: AsyncClient,
    db_session: AsyncSession,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
) -> None:
    """Per ``_scanned_counts_for_ticket_types``'s documented
    belt-and-suspenders filter: a scanned ticket belonging to a
    cancelled/expired order must not be counted, even though this
    shouldn't occur in practice."""
    await _login_admin(client, make_admin_user)
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=10)

    # A real, live paid+scanned ticket.
    await _make_order_with_tickets(
        db_session,
        event_id=event.id,
        ticket_type_id=ticket_type.id,
        status=OrderStatus.PAID,
        quantity=1,
        scanned_quantity=1,
    )
    # A "scanned" ticket on a cancelled order (shouldn't happen in
    # practice, but the filter must still hold).
    cancelled_order = await _make_order_with_tickets(
        db_session,
        event_id=event.id,
        ticket_type_id=ticket_type.id,
        status=OrderStatus.CANCELLED,
        quantity=1,
    )
    tickets_result = await db_session.execute(select(Ticket).where(Ticket.order_id == cancelled_order.id))
    cancelled_ticket = tickets_result.scalar_one()
    cancelled_ticket.scanned_at = datetime.now(UTC)
    await db_session.commit()

    response = await client.get(f"/api/v1/events/{event.id}/stats")

    assert response.status_code == 200, response.text
    body = response.json()
    tt_stats = body["shows"][0]["ticket_types"][0]
    assert tt_stats["scanned"] == 1
    assert body["scanned_total"] == 1


# --- revenue_by_payment_method: always both entries, zero-filled -----------


async def test_door_only_event_still_returns_a_zeroed_mollie_entry(
    client: AsyncClient,
    db_session: AsyncSession,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
) -> None:
    await _login_admin(client, make_admin_user)
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=10)
    await _make_order_with_tickets(
        db_session,
        event_id=event.id,
        ticket_type_id=ticket_type.id,
        status=OrderStatus.PAID,
        payment_method=PaymentMethod.DOOR,
        quantity=2,
        total=Decimal("20.00"),
    )

    response = await client.get(f"/api/v1/events/{event.id}/stats")

    assert response.status_code == 200, response.text
    revenue_by_method = {
        entry["payment_method"]: entry for entry in response.json()["revenue_by_payment_method"]
    }
    assert revenue_by_method["door"]["revenue"] == "20.00"
    assert revenue_by_method["door"]["order_count"] == 1
    assert revenue_by_method["mollie"]["revenue"] == "0.00"
    assert revenue_by_method["mollie"]["order_count"] == 0


async def test_mollie_only_event_still_returns_a_zeroed_door_entry(
    client: AsyncClient,
    db_session: AsyncSession,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
) -> None:
    await _login_admin(client, make_admin_user)
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=10)
    await _make_order_with_tickets(
        db_session,
        event_id=event.id,
        ticket_type_id=ticket_type.id,
        status=OrderStatus.PAID,
        payment_method=PaymentMethod.MOLLIE,
        quantity=1,
        total=Decimal("15.00"),
        mollie_payment_id="tr_only_mollie",
    )

    response = await client.get(f"/api/v1/events/{event.id}/stats")

    assert response.status_code == 200, response.text
    revenue_by_method = {
        entry["payment_method"]: entry for entry in response.json()["revenue_by_payment_method"]
    }
    assert revenue_by_method["mollie"]["revenue"] == "15.00"
    assert revenue_by_method["mollie"]["order_count"] == 1
    assert revenue_by_method["door"]["revenue"] == "0.00"
    assert revenue_by_method["door"]["order_count"] == 0


# --- GET /orders/export.csv -------------------------------------------------


def _parse_csv(csv_bytes: bytes) -> tuple[list[str], list[dict[str, str]]]:
    text = csv_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    assert reader.fieldnames is not None
    return list(reader.fieldnames), list(reader)


async def test_export_csv_returns_404_for_malformed_event_id(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login_admin(client, make_admin_user)

    response = await client.get("/api/v1/events/not-a-uuid/orders/export.csv")

    assert response.status_code == 404


async def test_export_csv_returns_404_for_unknown_event_id(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login_admin(client, make_admin_user)

    response = await client.get(f"/api/v1/events/{uuid.uuid4()}/orders/export.csv")

    assert response.status_code == 404


async def test_export_csv_headers_content_type_and_disposition(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
) -> None:
    await _login_admin(client, make_admin_user)
    event = await make_event(slug="my-test-event")

    response = await client.get(f"/api/v1/events/{event.id}/orders/export.csv")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == 'attachment; filename="orders-my-test-event.csv"'


async def test_export_csv_header_row_matches_csv_columns_exactly(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
) -> None:
    from app.services.stats import CSV_COLUMNS

    await _login_admin(client, make_admin_user)
    event = await make_event()

    response = await client.get(f"/api/v1/events/{event.id}/orders/export.csv")

    fieldnames, _ = _parse_csv(response.content)
    assert fieldnames == list(CSV_COLUMNS)


async def test_export_csv_is_unfiltered_by_status_and_includes_ticket_type_summary_and_blank_optionals(
    client: AsyncClient,
    db_session: AsyncSession,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
) -> None:
    await _login_admin(client, make_admin_user)
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    adult = await make_ticket_type(show_id=show.id, name="Adult", price=Decimal("20.00"), quantity_available=50)
    child = await make_ticket_type(show_id=show.id, name="Child", price=Decimal("10.00"), quantity_available=50)

    paid_order = Order(
        event_id=event.id,
        buyer_name="Paid Buyer",
        buyer_email="paid@example.test",
        buyer_address="1 Test Street",
        status=OrderStatus.PAID,
        payment_method=PaymentMethod.MOLLIE,
        mollie_payment_id="tr_export_paid",
        total=Decimal("50.00"),
        language="en",
    )
    db_session.add(paid_order)
    await db_session.flush()
    db_session.add_all(
        [
            Ticket(order_id=paid_order.id, ticket_type_id=adult.id),
            Ticket(order_id=paid_order.id, ticket_type_id=adult.id),
            Ticket(order_id=paid_order.id, ticket_type_id=child.id),
        ]
    )

    # A non-paid order, with neither mollie_payment_id nor an invoice —
    # must still appear, with its real status, and blank (not "None")
    # optional columns.
    cancelled_order = Order(
        event_id=event.id,
        buyer_name="Cancelled Buyer",
        buyer_email="cancelled@example.test",
        buyer_address="1 Test Street",
        status=OrderStatus.CANCELLED,
        payment_method=PaymentMethod.DOOR,
        total=Decimal("10.00"),
        language="en",
    )
    db_session.add(cancelled_order)
    await db_session.flush()
    db_session.add(Ticket(order_id=cancelled_order.id, ticket_type_id=child.id))
    await db_session.commit()

    response = await client.get(f"/api/v1/events/{event.id}/orders/export.csv")

    assert response.status_code == 200, response.text
    _, rows = _parse_csv(response.content)
    rows_by_order_id = {row["order_id"]: row for row in rows}

    assert str(paid_order.id) in rows_by_order_id
    assert str(cancelled_order.id) in rows_by_order_id

    paid_row = rows_by_order_id[str(paid_order.id)]
    assert paid_row["status"] == "paid"
    assert paid_row["ticket_types"] == "Adult x2; Child x1"
    assert paid_row["mollie_payment_id"] == "tr_export_paid"
    assert paid_row["invoice_number"] == ""  # no Invoice issued in this test
    assert paid_row["total"] == "50.00"

    cancelled_row = rows_by_order_id[str(cancelled_order.id)]
    assert cancelled_row["status"] == "cancelled"  # unfiltered by status
    assert cancelled_row["ticket_types"] == "Child x1"
    assert cancelled_row["mollie_payment_id"] == ""
    assert cancelled_row["invoice_number"] == ""
    assert "None" not in cancelled_row.values()

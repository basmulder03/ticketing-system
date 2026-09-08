"""Direct coverage of ``app.services.invoicing.issue_invoice_for_order``
(Milestone 5) against a real Postgres DB — idempotency (a caller-bug-style
double call, independent of the ``already_paid`` gate every real caller is
supposed to respect) and the snapshot-vs-live behavior
``app.models.invoice.Invoice``'s module docstring documents.

Needs a real DB (row-locked ``EventConfig`` allocation, unique constraints),
so this lives in ``tests/integration/`` rather than ``tests/unit/`` — same
reasoning as ``tests/integration/test_stock_service.py``. THE concurrency
test for sequential numbering lives separately in
``test_invoicing_concurrency.py``; gating-correctness-at-the-webhook-level
lives in ``test_invoicing_gating.py``.
"""

import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLogEntry
from app.models.enums import OrderStatus, PaymentMethod, PublishStatus
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.invoice import Invoice
from app.models.order import Order
from app.models.show import Show
from app.models.ticket import Ticket
from app.models.ticket_type import TicketType
from app.services.invoicing import issue_invoice_for_order
from app.services.order_payment import SYSTEM_PRINCIPAL

_ISSUED_ACTION = "invoice.issued"


async def _make_paid_order_with_tickets(
    session: AsyncSession,
    *,
    event_id: uuid.UUID,
    ticket_type_id: uuid.UUID,
    quantity: int = 1,
    total: Decimal = Decimal("15.00"),
    buyer_name: str = "Buyer",
    buyer_address: str = "1 Test Street",
) -> Order:
    order = Order(
        event_id=event_id,
        buyer_name=buyer_name,
        buyer_email="buyer@example.test",
        buyer_address=buyer_address,
        status=OrderStatus.PAID,
        payment_method=PaymentMethod.MOLLIE,
        total=total,
        language="en",
    )
    session.add(order)
    await session.flush()
    for _ in range(quantity):
        session.add(Ticket(order_id=order.id, ticket_type_id=ticket_type_id))
    await session.commit()
    await session.refresh(order)
    return order


async def _invoice_count_for_order(session: AsyncSession, order_id: uuid.UUID) -> int:
    result = await session.execute(select(func.count()).select_from(Invoice).where(Invoice.order_id == order_id))
    return result.scalar_one()


async def _audit_count(session: AsyncSession, *, action: str, target_id: str) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(AuditLogEntry)
        .where(AuditLogEntry.action == action)
        .where(AuditLogEntry.target_id == target_id)
    )
    return result.scalar_one()


# --- Idempotency (item 2): a caller-bug-style double call -------------------


async def test_issuing_twice_for_the_same_order_returns_the_identical_invoice(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, price=Decimal("15.00"))
    await make_event_config(event_id=event.id, invoice_number_prefix="CP-")
    order = await _make_paid_order_with_tickets(db_session, event_id=event.id, ticket_type_id=ticket_type.id)

    first = await issue_invoice_for_order(db_session, order=order, principal=SYSTEM_PRINCIPAL)
    await db_session.commit()

    second = await issue_invoice_for_order(db_session, order=order, principal=SYSTEM_PRINCIPAL)
    await db_session.commit()

    assert first.id == second.id
    assert first.number == second.number == 1
    assert first.formatted_number == second.formatted_number == "CP-00001"

    assert await _invoice_count_for_order(db_session, order.id) == 1
    assert await _audit_count(db_session, action=_ISSUED_ACTION, target_id=str(first.id)) == 1

    config_result = await db_session.execute(select(EventConfig).where(EventConfig.event_id == event.id))
    config = config_result.scalar_one()
    # Only ONE number was ever allocated, despite two calls.
    assert config.next_invoice_number == 2


async def test_issuing_thrice_for_the_same_order_still_stays_idempotent(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id)
    await make_event_config(event_id=event.id)
    order = await _make_paid_order_with_tickets(db_session, event_id=event.id, ticket_type_id=ticket_type.id)

    numbers: set[int] = set()
    for _ in range(3):
        invoice = await issue_invoice_for_order(db_session, order=order, principal=SYSTEM_PRINCIPAL)
        await db_session.commit()
        numbers.add(invoice.number)

    assert numbers == {1}
    assert await _invoice_count_for_order(db_session, order.id) == 1


# --- Snapshot-vs-live behavior (item 3) --------------------------------------


async def test_company_vat_and_prefix_are_frozen_at_issuance_not_re_read_later(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, price=Decimal("15.00"))
    original_ticket_type_name = ticket_type.name
    await make_event_config(
        event_id=event.id,
        invoice_company_name="Original Co",
        invoice_company_address="1 Original Street",
        invoice_company_vat_number="NL000000000B01",
        invoice_number_prefix="ORIG-",
    )
    order = await _make_paid_order_with_tickets(db_session, event_id=event.id, ticket_type_id=ticket_type.id)

    invoice = await issue_invoice_for_order(db_session, order=order, principal=SYSTEM_PRINCIPAL)
    await db_session.commit()
    invoice_id = invoice.id

    # An admin edits the company/VAT/prefix settings AND the TicketType
    # price AFTER the invoice already went out.
    config_result = await db_session.execute(select(EventConfig).where(EventConfig.event_id == event.id))
    config = config_result.scalar_one()
    config.invoice_company_name = "New Co"
    config.invoice_company_address = "2 New Street"
    config.invoice_company_vat_number = "NL999999999B01"
    config.invoice_number_prefix = "NEW-"

    tt_result = await db_session.execute(select(TicketType).where(TicketType.id == ticket_type.id))
    live_ticket_type = tt_result.scalar_one()
    live_ticket_type.price = Decimal("999.00")
    live_ticket_type.name = "Renamed Type"
    await db_session.commit()

    # Re-fetch the SAME already-issued Invoice fresh from the DB.
    refetched_result = await db_session.execute(select(Invoice).where(Invoice.id == invoice_id))
    refetched = refetched_result.scalar_one()

    assert refetched.company_name == "Original Co"
    assert refetched.company_address == "1 Original Street"
    assert refetched.company_vat_number == "NL000000000B01"
    assert refetched.number_prefix == "ORIG-"
    assert refetched.formatted_number == "ORIG-00001"

    assert len(refetched.line_items) == 1
    line_item = refetched.line_items[0]
    assert line_item["unit_price"] == "15.00", "line item price must stay frozen at issuance, not the new 999.00"
    assert line_item["line_total"] == "15.00"
    assert line_item["name"] == original_ticket_type_name, "line item name must stay frozen, not 'Renamed Type'"
    assert line_item["name"] != "Renamed Type"


async def test_buyer_name_address_and_total_are_read_live_off_order_not_snapshotted(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """Verifies ``app.models.invoice.Invoice``'s docstring claim directly,
    rather than assuming it: buyer name/address/total are NOT stored on the
    Invoice row at all, so re-rendering after a direct Order mutation must
    reflect the NEW values — there is no separate "frozen buyer" field that
    could disagree with ``Order``."""
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, price=Decimal("15.00"))
    await make_event_config(event_id=event.id)
    order = await _make_paid_order_with_tickets(
        db_session,
        event_id=event.id,
        ticket_type_id=ticket_type.id,
        buyer_name="Original Buyer",
        buyer_address="1 Original Street",
        total=Decimal("15.00"),
    )

    invoice = await issue_invoice_for_order(db_session, order=order, principal=SYSTEM_PRINCIPAL)
    await db_session.commit()

    # Invoice itself has no buyer_name/buyer_address/total column at all —
    # renders always go through the live Order relationship (see
    # app.services.invoice_pdf.render_invoice_pdf / _invoice_html).
    assert not hasattr(invoice, "buyer_name")
    assert not hasattr(invoice, "buyer_address")

    order_result = await db_session.execute(select(Order).where(Order.id == order.id))
    live_order = order_result.scalar_one()
    live_order.buyer_name = "Updated Buyer"
    live_order.buyer_address = "2 Updated Street"
    live_order.total = Decimal("42.00")
    await db_session.commit()

    refetched_order_result = await db_session.execute(select(Order).where(Order.id == order.id))
    refetched_order = refetched_order_result.scalar_one()
    assert refetched_order.buyer_name == "Updated Buyer"
    assert refetched_order.buyer_address == "2 Updated Street"
    assert refetched_order.total == Decimal("42.00")

    # The Invoice's own snapshot fields are untouched by the Order edit.
    refetched_invoice_result = await db_session.execute(select(Invoice).where(Invoice.id == invoice.id))
    refetched_invoice = refetched_invoice_result.scalar_one()
    assert refetched_invoice.order_id == order.id


async def test_rendered_invoice_html_combines_frozen_snapshot_with_live_order_fields(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """End-to-end proof of the snapshot-vs-live split, through the actual
    render step (``app.services.invoice_pdf.render_invoice_pdf``) against
    real, DB-round-tripped rows — not just asserting on stored column
    values in isolation, per this milestone's guidance to verify the claim
    rather than assume it."""
    from app.services.invoice_pdf import _invoice_html

    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, name="Adult", price=Decimal("15.00"))
    await make_event_config(
        event_id=event.id, invoice_company_name="Original Co", invoice_number_prefix="ORIG-"
    )
    order = await _make_paid_order_with_tickets(
        db_session, event_id=event.id, ticket_type_id=ticket_type.id, buyer_name="Original Buyer"
    )

    invoice = await issue_invoice_for_order(db_session, order=order, principal=SYSTEM_PRINCIPAL)
    await db_session.commit()

    # Edit config/TicketType (should NOT show up) and Order.buyer_name
    # (SHOULD show up) after issuance.
    config_result = await db_session.execute(select(EventConfig).where(EventConfig.event_id == event.id))
    config = config_result.scalar_one()
    config.invoice_company_name = "Changed Co"
    tt_result = await db_session.execute(select(TicketType).where(TicketType.id == ticket_type.id))
    live_ticket_type = tt_result.scalar_one()
    live_ticket_type.price = Decimal("999.00")
    order_result = await db_session.execute(select(Order).where(Order.id == order.id))
    live_order = order_result.scalar_one()
    live_order.buyer_name = "Updated Buyer"
    await db_session.commit()

    event_result = await db_session.execute(select(Event).where(Event.id == event.id))
    fresh_event = event_result.scalar_one()
    invoice_result = await db_session.execute(select(Invoice).where(Invoice.id == invoice.id))
    fresh_invoice = invoice_result.scalar_one()
    order_result = await db_session.execute(select(Order).where(Order.id == order.id))
    fresh_order = order_result.scalar_one()

    html = _invoice_html(invoice=fresh_invoice, order=fresh_order, event=fresh_event, theme=None, locale="en")

    assert "Original Co" in html, "company name must stay frozen at issue time"
    assert "Changed Co" not in html
    assert "€15.00" in html or "15,00" in html, "unit price must stay frozen, not the new 999.00"
    assert "999.00" not in html
    assert "Updated Buyer" in html, "buyer name must reflect the live Order row"
    assert "Original Buyer" not in html

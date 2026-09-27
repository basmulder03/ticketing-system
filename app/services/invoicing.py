"""Sequential invoice numbering and idempotent issuance.

:func:`issue_invoice_for_order` is called by every payment-confirmation
path, only after a fresh ``mark_order_paid`` (``already_paid is False``),
inside the same transaction — so if that transaction rolls back, the
number isn't burned. Numbers are allocated under a ``FOR UPDATE`` lock on
the event's ``EventConfig``, so concurrent payments never share a number.
"""

import uuid
from typing import TypedDict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import Principal
from app.models.event_config import EventConfig
from app.models.invoice import Invoice
from app.models.order import Order
from app.models.ticket import Ticket
from app.models.ticket_type import TicketType
from app.services.audit import record_audit_entry

__all__ = ["LineItem", "issue_invoice_for_order"]


class LineItem(TypedDict):
    """One line per ticket type; money as decimal strings."""

    name: str
    quantity: int
    unit_price: str
    line_total: str


async def _allocate_invoice_number(session: AsyncSession, *, event_id: uuid.UUID) -> tuple[int, EventConfig | None]:
    """Lock the event's ``EventConfig``, take ``next_invoice_number`` and bump it.

    Returns ``(number, locked_config)`` so the caller can snapshot company
    details while it holds the lock. With no EventConfig at all (not reachable
    through normal event creation) it falls back to number 1.
    """
    # populate_existing() is load-bearing: the webhook already loaded this
    # EventConfig in the same session, and without it the identity map returns
    # the stale pre-lock next_invoice_number — concurrent payments would then
    # get the same number despite the lock.
    result = await session.execute(
        select(EventConfig)
        .where(EventConfig.event_id == event_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    config = result.scalar_one_or_none()
    if config is None:
        return 1, None
    number = config.next_invoice_number
    config.next_invoice_number = number + 1
    return number, config


def _build_line_items(tickets: list[Ticket], ticket_types_by_id: dict[str, TicketType]) -> list[LineItem]:
    """Group tickets by type into line items, freezing current names and prices."""
    quantities: dict[str, int] = {}
    for ticket in tickets:
        key = str(ticket.ticket_type_id)
        quantities[key] = quantities.get(key, 0) + 1

    items: list[LineItem] = []
    for ticket_type_id, quantity in quantities.items():
        ticket_type = ticket_types_by_id[ticket_type_id]
        unit_price = ticket_type.price
        line_total = unit_price * quantity
        items.append(
            LineItem(
                name=ticket_type.name,
                quantity=quantity,
                unit_price=str(unit_price),
                line_total=str(line_total),
            )
        )
    return items


async def _load_tickets_with_types(session: AsyncSession, *, order_id: uuid.UUID) -> list[Ticket]:
    """Order's tickets with their types eager-loaded (no lazy loads in async)."""
    result = await session.execute(
        select(Ticket).where(Ticket.order_id == order_id).options(selectinload(Ticket.ticket_type))
    )
    return list(result.scalars().all())


async def issue_invoice_for_order(
    session: AsyncSession,
    *,
    order: Order,
    principal: Principal,
) -> Invoice:
    """Issue the order's one invoice, or return the existing one.

    The existing-invoice check plus the unique ``order_id`` constraint make a
    duplicate impossible even if a caller forgets the ``already_paid`` gate.
    Flushes but doesn't commit.
    """
    existing = await session.execute(select(Invoice).where(Invoice.order_id == order.id))
    invoice = existing.scalar_one_or_none()
    if invoice is not None:
        return invoice

    tickets = await _load_tickets_with_types(session, order_id=order.id)
    ticket_types_by_id = {str(t.ticket_type_id): t.ticket_type for t in tickets}

    number, config = await _allocate_invoice_number(session, event_id=order.event_id)
    line_items = _build_line_items(tickets, ticket_types_by_id)

    invoice = Invoice(
        order_id=order.id,
        event_id=order.event_id,
        number=number,
        number_prefix=(config.invoice_number_prefix or "") if config is not None else "",
        company_name=config.invoice_company_name if config is not None else None,
        company_address=config.invoice_company_address if config is not None else None,
        company_vat_number=config.invoice_company_vat_number if config is not None else None,
        line_items=list(line_items),
    )
    session.add(invoice)
    await session.flush()

    await record_audit_entry(
        session,
        principal,
        action="invoice.issued",
        target_type="Invoice",
        target_id=str(invoice.id),
        detail={"order_id": str(order.id), "number": invoice.formatted_number},
    )
    await session.flush()
    return invoice

"""Concurrency-safe sequential invoice numbering and idempotent Invoice
issuance (Milestone 5), per PROJECT_BRIEF.md's Invoicing section.

:func:`issue_invoice_for_order` is the single entry point every payment-
confirmation path calls — the Mollie webhook and the preview-mode
simulated-checkout path today (see ``app.api.routes.public``), and (once
Milestone 6 wires manual mark-as-paid through the same
``app.services.order_payment.mark_order_paid`` function) door/manual
reconciliation automatically, with no extra code needed here. Every caller
MUST gate the call on ``MarkOrderPaidResult.already_paid is False`` (the
exact same rule Milestone 4's ticket-email/QR-signing dispatch already
follows) — a duplicate invoice number is a real accounting problem, not
just a UX one, so this module also defends itself at a second layer (see
:func:`issue_invoice_for_order`'s docstring) rather than relying solely on
callers remembering the gate correctly.

Numbering discipline mirrors ``app.services.stock.reserve_stock``'s
row-locking pattern: :func:`_allocate_invoice_number` row-locks the Event's
``EventConfig`` (``SELECT ... FOR UPDATE``) before reading/incrementing
``next_invoice_number``, so two concurrent payment confirmations for the
same Event can never read the same "next" value — the second caller blocks
at the locking query until the first transaction commits or rolls back.
Called from INSIDE the same DB transaction as the ``mark_order_paid``
status flip (flush-only, no commit) — exactly like Milestone 4's
``sign_order_tickets`` — so number allocation is atomic with the payment
confirmation itself: if the surrounding transaction rolls back for any
reason, the allocated number rolls back with it and is never "burned"
without a corresponding Invoice row.
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
    """One invoice line item, one per distinct ``TicketType`` purchased.
    ``unit_price``/``line_total`` are decimal strings (never float — see
    ``app.models.invoice.Invoice.line_items`` docstring for why JSON
    storage uses strings rather than a numeric JSON type)."""

    name: str
    quantity: int
    unit_price: str
    line_total: str


async def _allocate_invoice_number(session: AsyncSession, *, event_id: uuid.UUID) -> tuple[int, EventConfig | None]:
    """Row-lock ``event_id``'s ``EventConfig`` and atomically allocate the
    next sequential invoice number, incrementing
    ``EventConfig.next_invoice_number`` for the next caller.

    Returns ``(allocated_number, locked_config)`` — the caller reads the
    company/VAT/prefix snapshot fields off ``locked_config`` while it's
    still locked, avoiding a second round trip. If the Event genuinely has
    no ``EventConfig`` row at all (should not happen for an event that has
    taken a real paid order, but defensively handled rather than crashing a
    real payment confirmation), falls back to number ``1`` with no config
    to snapshot from — every subsequent invoice for that same
    still-configless event would also get ``1`` and collide at the DB's
    unique constraint, but that failure mode requires an operator to have
    published an event with no EventConfig row at all, which the rest of
    this app's Event-creation flow does not allow to happen.
    """
    # populate_existing() is load-bearing, not a stylistic default: the
    # webhook route (app.api.routes.public.mollie_webhook) already loads
    # this same Event's EventConfig earlier in the SAME session (via
    # `session.get(Event, ..., options=[selectinload(Event.config)])`, to
    # resolve the Mollie API key) BEFORE this function ever runs. Without
    # populate_existing(), SQLAlchemy's identity map returns that
    # already-loaded Python object as-is when this query's WHERE clause
    # matches the same primary key — the SELECT ... FOR UPDATE is still
    # genuinely sent to Postgres and genuinely serializes concurrent
    # transactions at the DB level, but the in-memory `next_invoice_number`
    # attribute is NOT refreshed from that query's result, so every
    # transaction reads the SAME stale pre-lock value it cached earlier
    # instead of the fresh, correctly-serialized one — multiple concurrent
    # payment confirmations then compute the identical "next" number
    # despite the row lock working correctly, and collide on
    # Invoice's (event_id, number) unique constraint. Reproduced and
    # confirmed via a minimal isolated script before this fix; re-verified
    # after (see this module's test coverage).
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
    """Group ``tickets`` by ``TicketType`` into one :class:`LineItem` per
    distinct type purchased on the order (name, quantity, unit price, line
    total) — the shape PROJECT_BRIEF.md's Invoicing section calls for
    ("line items (one per TicketType purchased)").

    Reads ``TicketType.price``/``name`` live at issuance time (the moment
    this function is called, always inside the same transaction as payment
    confirmation) and freezes them into the returned snapshot — never
    re-read again after this point, per ``app.models.invoice.Invoice``'s
    module docstring.
    """
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
    """Fetch every ``Ticket`` belonging to ``order_id`` with its
    ``TicketType`` eagerly loaded (``selectinload``) — required because
    :func:`_build_line_items` reads ``ticket.ticket_type.price``/``.name``,
    and lazy-loading a relationship is not safe on this project's async
    SQLAlchemy session."""
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
    """Idempotently issue the one-and-only Invoice for ``order``.

    Callers MUST already be inside the same transaction as a fresh (never
    previously observed) ``mark_order_paid`` transition, per this module's
    docstring. As a second, independent idempotency layer (defense in depth
    beyond callers correctly gating on ``already_paid``), this function
    itself first checks for an already-existing Invoice row for
    ``order.id`` and returns it unchanged if found — so even a caller bug
    that invokes this twice for the same Order can never allocate a second
    number or double-write the audit log. Combined with
    ``Invoice.order_id``'s DB-level ``UNIQUE`` constraint, a duplicate
    invoice for one Order is not reachable even under a race between two
    callers that both pass the ``already_paid`` gate (the loser of the
    ``EventConfig`` row lock in :func:`_allocate_invoice_number` blocks
    until the winner commits, then would find some existing Invoice row on
    a re-check — in practice unreachable anyway because ``mark_order_paid``
    itself already row-locks the Order first, serializing any two callers
    for the same Order before either reaches this function at all).

    Flushes but does not commit — the caller controls the transaction
    boundary, exactly like ``app.services.ticket_delivery.sign_order_tickets``.
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

"""Order actions and reads. Admin-only throughout: orders are financial and
buyer PII, never agent-accessible content.

Routers are split by URL shape: ``router`` (``/orders/{id}/...``),
``list_router`` (``/events/{id}/orders``) and ``show_router``
(``/shows/{id}/...`` — batch printing and manual orders, which return order
data and so belong here rather than in the agent-accessible shows module).
"""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import Principal, require_admin
from app.api.routes._utils import parse_uuid_or_404
from app.db.session import get_session
from app.models.enums import OrderStatus
from app.models.event import Event
from app.models.order import Order
from app.models.show import Show
from app.models.ticket import Ticket
from app.models.ticket_type import TicketType
from app.schemas.order import (
    ErasePiiRequest,
    ErasePiiResponse,
    ManualOrderCreateRequest,
    MarkOrderPaidRequest,
    MarkOrderPaidResponse,
    OrderOut,
    TicketOut,
)
from app.services.gdpr import erase_order_pii
from app.services.invoice_pdf import render_invoice_pdf
from app.services.invoicing import issue_invoice_for_order
from app.services.manual_order import ManualOrderError, ManualOrderItemInput, create_manual_order
from app.services.order_payment import mark_order_paid
from app.services.pricing import derive_order_subtotal_and_fee
from app.services.stock import InsufficientStockError, TicketTypeNotFoundError
from app.services.ticket_delivery import send_order_confirmation_email, sign_order_tickets
from app.services.ticket_pdf import render_tickets_pdf, render_tickets_pdf_batch

router = APIRouter(prefix="/api/v1/orders", tags=["orders"])
list_router = APIRouter(prefix="/api/v1/events/{event_id}/orders", tags=["orders"])
show_router = APIRouter(prefix="/api/v1/shows", tags=["orders"])


async def _order_to_out(session: AsyncSession, order: Order) -> OrderOut:
    result = await session.execute(
        select(Ticket).where(Ticket.order_id == order.id).options(selectinload(Ticket.ticket_type))
    )
    tickets = result.scalars().unique().all()
    subtotal, service_fee_total = derive_order_subtotal_and_fee(order.total, tickets)
    return OrderOut(
        id=str(order.id),
        event_id=str(order.event_id),
        status=order.status,
        payment_method=order.payment_method,
        buyer_name=order.buyer_name,
        buyer_email=order.buyer_email,
        buyer_address=order.buyer_address,
        language=order.language,
        total=order.total,
        subtotal=subtotal,
        service_fee_total=service_fee_total,
        tickets=[
            TicketOut(
                id=str(t.id),
                ticket_type_id=str(t.ticket_type_id),
                ticket_type_name=t.ticket_type.name,
                price=t.ticket_type.price,
                qr_token=t.qr_token,
            )
            for t in tickets
        ],
        created_at=order.created_at,
        payment_redirect_url=None,
    )


@list_router.get("")
async def list_orders(
    event_id: str,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[OrderOut]:
    """The event's orders, newest first. No filtering or paging."""
    parsed_event_id = parse_uuid_or_404(event_id, detail="Event not found.")
    event = await session.get(Event, parsed_event_id)
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found.")

    result = await session.execute(
        select(Order).where(Order.event_id == event.id).order_by(Order.created_at.desc())
    )
    orders = result.scalars().all()
    return [await _order_to_out(session, order) for order in orders]


@router.post("/{order_id}/resend-confirmation-email")
async def resend_confirmation_email(
    order_id: str,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    """Resend the confirmation email for a paid order (days-until-show recomputed).
    404 if missing, 409 if not paid. ``{"sent": false}`` means the send failed;
    the reason is already in the audit log.
    """
    parsed_id = parse_uuid_or_404(order_id, detail="Order not found.")
    order = await session.get(Order, parsed_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")
    if order.status != OrderStatus.PAID:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only paid orders have a confirmation email to resend.",
        )

    sent = await send_order_confirmation_email(
        session, order_id=order.id, principal=principal, trigger="manual_resend"
    )
    return {"sent": sent}


@router.post("/{order_id}/mark-paid")
async def mark_paid(
    order_id: str,
    body: MarkOrderPaidRequest,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> MarkOrderPaidResponse:
    """Manually mark an order paid (cash, card terminal, bank transfer, corrections).

    Goes through ``mark_order_paid``, attributed to the real admin (never
    ``SYSTEM_PRINCIPAL``). Works from any non-paid status. A fresh transition
    triggers the same effects as the webhook: sign tickets and issue the invoice
    in this transaction, commit, then email. A repeat call is a no-op. 409 when
    reviving a cancelled/expired order whose stock was resold — a real conflict
    for staff to resolve.
    """
    parsed_id = parse_uuid_or_404(order_id, detail="Order not found.")
    order = await session.get(Order, parsed_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")

    try:
        result = await mark_order_paid(
            session,
            order_id=parsed_id,
            principal=principal,
            method_label=body.method_label,
            reason=body.reason,
        )
    except InsufficientStockError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Cannot mark this order as paid: its stock was released when it was cancelled/expired "
                f"and only {max(exc.remaining, 0)} of {exc.requested} requested ticket(s) remain available "
                "for one of its ticket types. It may have already been sold to a different buyer."
            ),
        ) from exc
    except TicketTypeNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot mark this order as paid: one or more of its ticket types no longer exist.",
        ) from exc
    if not result.already_paid:
        # Same fresh-payment effects as the Mollie webhook, in this transaction.
        await sign_order_tickets(session, order=result.order)
        await issue_invoice_for_order(session, order=result.order, principal=principal)

    await session.commit()

    if not result.already_paid:
        # After commit, best-effort: an email failure must never undo the payment.
        await send_order_confirmation_email(session, order_id=parsed_id, principal=principal)

    order_out = await _order_to_out(session, result.order)
    return MarkOrderPaidResponse(already_paid=result.already_paid, order=order_out)


@router.post("/{order_id}/erase-pii")
async def erase_pii(
    order_id: str,
    body: ErasePiiRequest,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ErasePiiResponse:
    """Anonymize the buyer's name, email and address (GDPR deletion request).
    The order, tickets and invoice stay for accounting.

    An invoiced order needs ``confirm: true`` (invoices may have to be kept for
    years); without it this returns 409 explaining why, so the admin decides
    knowingly. Idempotent.
    """
    parsed_id = parse_uuid_or_404(order_id, detail="Order not found.")
    result = await session.execute(
        select(Order).where(Order.id == parsed_id).options(selectinload(Order.invoice))
    )
    order = result.scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")

    had_invoice = order.invoice is not None
    if had_invoice and not body.confirm:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This order has an issued invoice. Invoices (which may include the buyer's name and "
                "address) are typically subject to statutory accounting/tax retention periods — verify "
                "your own invoice-retention obligations before erasing this order's buyer details. "
                "Resubmit with confirm: true to proceed anyway."
            ),
        )

    await erase_order_pii(session, order=order, principal=principal)
    await session.commit()

    order_out = await _order_to_out(session, order)
    return ErasePiiResponse(order=order_out, had_invoice=had_invoice)


@router.get("/{order_id}/invoice.pdf")
async def download_invoice_pdf(
    order_id: str,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Re-download an issued invoice PDF, rendered fresh on each request. 404 if
    the order doesn't exist or has no invoice (i.e. isn't paid).
    """
    parsed_id = parse_uuid_or_404(order_id, detail="Order not found.")
    result = await session.execute(
        select(Order)
        .where(Order.id == parsed_id)
        .options(selectinload(Order.invoice), selectinload(Order.event).selectinload(Event.theme))
    )
    order = result.scalar_one_or_none()
    if order is None or order.event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")
    if order.invoice is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No invoice has been issued for this order yet."
        )

    pdf_bytes = render_invoice_pdf(
        invoice=order.invoice,
        order=order,
        event=order.event,
        theme=order.event.theme,
        locale=order.language,
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="invoice-{order.id}.pdf"'},
    )


@router.get("/{order_id}/tickets.pdf")
async def download_tickets_pdf(
    order_id: str,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Download an order's ticket PDF without resending the email. 404 if the order
    doesn't exist or isn't paid (no signed tickets). Same filename as the email
    attachment.
    """
    parsed_id = parse_uuid_or_404(order_id, detail="Order not found.")
    result = await session.execute(
        select(Order)
        .where(Order.id == parsed_id)
        .options(
            selectinload(Order.event).selectinload(Event.theme),
            selectinload(Order.tickets).selectinload(Ticket.ticket_type).selectinload(TicketType.show),
        )
    )
    order = result.scalar_one_or_none()
    if order is None or order.event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")
    if not order.tickets or any(ticket.qr_token is None for ticket in order.tickets):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No signed tickets have been issued for this order yet.",
        )

    tickets = list(order.tickets)
    show = tickets[0].ticket_type.show
    ticket_types_by_id = {str(ticket.ticket_type_id): ticket.ticket_type for ticket in tickets}

    pdf_bytes = render_tickets_pdf(
        order=order,
        tickets=tickets,
        ticket_types_by_id=ticket_types_by_id,
        show=show,
        event=order.event,
        theme=order.event.theme,
        locale=order.language,
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="tickets-{order.id}.pdf"'},
    )


@show_router.get("/{show_id}/tickets-batch.pdf")
async def download_show_tickets_batch_pdf(
    show_id: str,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """One PDF with every paid order's tickets for a show, for printing a run.

    404 for an unknown show or nothing to print. A paid order with an unsigned
    ticket (shouldn't happen) is skipped rather than failing the batch.
    """
    parsed_show_id = parse_uuid_or_404(show_id, detail="Show not found.")
    show_result = await session.execute(
        select(Show).where(Show.id == parsed_show_id).options(selectinload(Show.event).selectinload(Event.theme))
    )
    show = show_result.scalar_one_or_none()
    if show is None or show.event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Show not found.")

    orders_result = await session.execute(
        select(Order)
        .join(Ticket, Ticket.order_id == Order.id)
        .join(TicketType, TicketType.id == Ticket.ticket_type_id)
        .where(TicketType.show_id == show.id, Order.status == OrderStatus.PAID)
        .options(selectinload(Order.tickets).selectinload(Ticket.ticket_type))
        .order_by(Order.created_at)
        .distinct()
    )
    orders = orders_result.scalars().unique().all()

    orders_with_tickets: list[tuple[Order, list[Ticket], dict[str, TicketType]]] = []
    for order in orders:
        # Defensive: checkout already guarantees one show per order.
        show_tickets = [ticket for ticket in order.tickets if ticket.ticket_type.show_id == show.id]
        if not show_tickets or any(ticket.qr_token is None for ticket in show_tickets):
            continue
        ticket_types_by_id = {str(ticket.ticket_type_id): ticket.ticket_type for ticket in show_tickets}
        orders_with_tickets.append((order, show_tickets, ticket_types_by_id))

    if not orders_with_tickets:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No paid orders with issued tickets for this show yet.",
        )

    pdf_bytes = render_tickets_pdf_batch(
        orders_with_tickets=orders_with_tickets,
        show=show,
        event=show.event,
        theme=show.event.theme,
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="tickets-batch-{show.id}.pdf"'},
    )


@show_router.post("/{show_id}/manual-orders", status_code=status.HTTP_201_CREATED)
async def create_manual_order_route(
    show_id: str,
    body: ManualOrderCreateRequest,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> OrderOut:
    """Create and immediately settle an order for a buyer who never checked out
    (see ``app.services.manual_order``).

    Skips every buyer-facing sales gate, but stock is still enforced (409).
    Signs tickets and issues the invoice; emails only if an address was given —
    otherwise the admin downloads the PDFs and hands them over.
    """
    parsed_show_id = parse_uuid_or_404(show_id, detail="Show not found.")
    show = await session.get(Show, parsed_show_id)
    if show is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Show not found.")

    items = [
        ManualOrderItemInput(
            ticket_type_id=parse_uuid_or_404(item.ticket_type_id, detail="Ticket type not found."),
            quantity=item.quantity,
        )
        for item in body.items
    ]

    try:
        order = await create_manual_order(
            session,
            show_id=show.id,
            event_id=show.event_id,
            items=items,
            buyer_name=body.buyer_name,
            buyer_email=body.buyer_email,
            buyer_address=body.buyer_address,
            language=body.language,
            principal=principal,
            method_label=body.method_label,
            reason=body.reason,
        )
    except ManualOrderError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.detail) from exc

    await sign_order_tickets(session, order=order)
    await issue_invoice_for_order(session, order=order, principal=principal)
    await session.commit()

    if body.buyer_email:
        # After commit, best-effort (same as mark_paid).
        await send_order_confirmation_email(session, order_id=order.id, principal=principal)

    return await _order_to_out(session, order)

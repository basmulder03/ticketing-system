"""Admin-only Order actions and read access.

Admin-only (``require_admin``, not ``require_admin_or_agent``) throughout:
an Order is financial/buyer-PII data, not the "content-type data" (events,
shows, ticket types, theme fields, email template content) the brief
scopes agent keys to.

Two routers in this module, both admin-only, split by URL shape rather
than by concern: ``router`` (flat ``/api/v1/orders/{order_id}/...``) for
actions addressed by order id alone (resend); ``list_router`` (nested
``/api/v1/events/{event_id}/orders``) for listing an Event's orders,
mirroring Show/TicketType's nested-under-Event pattern. Added alongside
the Milestone 4 backoffice Orders view (``app.web.routes.orders``), which
needs somewhere to actually source order data from — no detail/update
routes beyond this yet (full order management/mark-as-paid is Milestone
6, stats/filtering is Milestone 8).
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
from app.models.ticket import Ticket
from app.schemas.order import OrderOut, TicketOut
from app.services.invoice_pdf import render_invoice_pdf
from app.services.ticket_delivery import send_order_confirmation_email

router = APIRouter(prefix="/api/v1/orders", tags=["orders"])
list_router = APIRouter(prefix="/api/v1/events/{event_id}/orders", tags=["orders"])


async def _order_to_out(session: AsyncSession, order: Order) -> OrderOut:
    result = await session.execute(
        select(Ticket).where(Ticket.order_id == order.id).options(selectinload(Ticket.ticket_type))
    )
    tickets = result.scalars().unique().all()
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
        mollie_checkout_url=None,
    )


@list_router.get("")
async def list_orders(
    event_id: str,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[OrderOut]:
    """List all Orders under the given Event, most recent first.

    Minimal by design (no filtering/sorting/pagination) — just enough for
    the Milestone 4 backoffice Orders view to list orders and offer the
    resend action; a real stats/filtering surface is Milestone 8 scope.
    """
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
    """Re-send the order-confirmation/ticket email for an already-``paid``
    Order, using the exact same rendering/send path as the automatic
    payment-confirmation dispatch (see
    ``app.services.ticket_delivery.send_order_confirmation_email``) —
    "time until the show" is recomputed fresh at send time, so a resend
    closer to the event date reflects the shorter remaining time, per
    PROJECT_BRIEF.md.

    404s if the Order doesn't exist; 409s if it exists but isn't ``paid``
    yet (there is nothing to resend — no tickets are meant to be issued for
    an order that hasn't been paid). Returns ``{"sent": true}`` on success;
    ``{"sent": false}`` if the send itself failed (SMTP misconfigured/
    unreachable) — the failure is already written to the audit log by
    :func:`send_order_confirmation_email`, so this response only needs to
    tell the caller whether to show a success or failure message, not
    what went wrong.
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


@router.get("/{order_id}/invoice.pdf")
async def download_invoice_pdf(
    order_id: str,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Direct backoffice PDF download/re-download of an already-issued
    Invoice — the "re-download" half of PROJECT_BRIEF.md's Invoicing
    section ("resend/re-download action in backoffice"), distinct from the
    resend-email action above (which re-sends the SAME email containing
    this same PDF as an attachment, per ``app.services.ticket_delivery``).

    Admin-only (financial data, not agent-key content-management scope).
    404s if the Order doesn't exist or has no Invoice yet (i.e. it isn't
    ``paid`` — an Invoice is only ever created by
    ``app.services.invoicing.issue_invoice_for_order`` at payment
    confirmation, never speculatively). Re-renders fresh from the Invoice's
    stored snapshot data on every call (same "render on demand, don't
    persist PDF bytes" pattern as ``app.services.ticket_pdf``) — always
    byte-for-byte reproducible since nothing an Invoice snapshots can
    change after issuance (see ``app.models.invoice.Invoice`` docstring).
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

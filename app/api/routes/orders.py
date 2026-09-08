"""Admin-only Order actions and read access.

Admin-only (``require_admin``, not ``require_admin_or_agent``) throughout:
an Order is financial/buyer-PII data, not the "content-type data" (events,
shows, ticket types, theme fields, email template content) the brief
scopes agent keys to.

Two routers in this module, both admin-only, split by URL shape rather
than by concern: ``router`` (flat ``/api/v1/orders/{order_id}/...``) for
actions addressed by order id alone (resend, invoice download,
Milestone 6's manual mark-as-paid); ``list_router`` (nested
``/api/v1/events/{event_id}/orders``) for listing an Event's orders,
mirroring Show/TicketType's nested-under-Event pattern. Added alongside
the Milestone 4 backoffice Orders view (``app.web.routes.orders``), which
needs somewhere to actually source order data from — stats/filtering is
Milestone 8 scope.
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
from app.schemas.order import MarkOrderPaidRequest, MarkOrderPaidResponse, OrderOut, TicketOut
from app.services.invoice_pdf import render_invoice_pdf
from app.services.invoicing import issue_invoice_for_order
from app.services.order_payment import mark_order_paid
from app.services.stock import InsufficientStockError, TicketTypeNotFoundError
from app.services.ticket_delivery import send_order_confirmation_email, sign_order_tickets

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


@router.post("/{order_id}/mark-paid")
async def mark_paid(
    order_id: str,
    body: MarkOrderPaidRequest,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> MarkOrderPaidResponse:
    """Manually mark an Order as ``paid`` — the backoffice counterpart to
    the automatic Mollie webhook confirmation, per PROJECT_BRIEF.md's
    Manual Payment Handling section ("Backoffice action to manually mark
    any order as paid, regardless of original payment method — covers door
    card payments (via a separately-operated SumUp terminal), bank
    transfers, true cash, corrections, etc.").

    Delegates the actual status transition to
    ``app.services.order_payment.mark_order_paid`` — the single row-locked,
    idempotent entry point also used by the Mollie webhook — attributed to
    the REAL logged-in admin ``principal`` (never ``SYSTEM_PRINCIPAL``,
    which is reserved for automated transitions; see that module's
    docstring for why the distinction matters to this app's audit model).

    Works from ANY non-``paid`` starting status (``pending``,
    ``pending_door``, and even a previously ``cancelled``/``expired``
    order), not just ``pending_door``: PROJECT_BRIEF.md's own wording
    ("mark ANY order as paid, regardless of original payment method... or
    corrections") reads as intentionally broad, and ``mark_order_paid``'s
    own docstring already anticipates this ("a manual override of a lapsed
    order is a legitimate staff action") — so no extra status gate is
    added here beyond what that function already enforces.

    On a genuinely fresh transition (``already_paid`` is ``False`` in the
    response), triggers the exact same downstream effects as the webhook,
    in the same order and with the same transaction boundary: sign the
    order's ticket QR tokens and issue its Invoice inside this same
    transaction, commit, and only THEN (best-effort, after commit, never
    able to roll back the payment confirmation) send the order-confirmation
    email with both PDFs attached. On a repeat call for an already-``paid``
    Order (``already_paid`` is ``True``), none of that re-runs — no
    duplicate email, no duplicate audit entry — so this action is safe to
    click more than once.

    404s if the Order doesn't exist, matching every other route in this
    module. 409s if this Order is a ``cancelled``/``expired`` recovery
    whose stock has since been resold to someone else — see
    ``app.services.order_payment._verify_stock_for_resurrected_order``;
    this is a genuine capacity conflict, not a bug, and the caller must
    resolve it manually (e.g. contact the buyer) rather than retry.
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
        # Milestones 4/5: same fresh-payment effects as the Mollie webhook,
        # inside this same transaction — see
        # ``app.api.routes.public.mollie_webhook`` for the sequence this
        # mirrors.
        await sign_order_tickets(session, order=result.order)
        await issue_invoice_for_order(session, order=result.order, principal=principal)

    await session.commit()

    if not result.already_paid:
        # Deliberately after the commit above, best-effort — an SMTP
        # failure must never undo or fail this mark-as-paid action itself,
        # matching the webhook's own error-handling discipline.
        await send_order_confirmation_email(session, order_id=parsed_id, principal=principal)

    order_out = await _order_to_out(session, result.order)
    return MarkOrderPaidResponse(already_paid=result.already_paid, order=order_out)


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

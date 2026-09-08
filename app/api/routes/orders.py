"""Admin-only Order actions. Milestone 4 adds exactly one: resending an
order's confirmation/ticket email — PROJECT_BRIEF.md's Ticket Generation &
Delivery section: "Backoffice action to resend a ticket".

Admin-only (``require_admin``, not ``require_admin_or_agent``): an Order is
financial/buyer-PII data, not the "content-type data" (events, shows,
ticket types, theme fields, email template content) the brief scopes agent
keys to.

No general Order listing/detail CRUD is added here — out of this
milestone's scope (flagged in the Milestone 4 handoff for whichever
milestone builds backoffice order management/stats, since a resend button
needs somewhere to find an order id from first).
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, require_admin
from app.api.routes._utils import parse_uuid_or_404
from app.db.session import get_session
from app.models.enums import OrderStatus
from app.models.order import Order
from app.services.ticket_delivery import send_order_confirmation_email

router = APIRouter(prefix="/api/v1/orders", tags=["orders"])


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

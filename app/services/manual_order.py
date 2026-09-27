"""Admin-issued orders for buyers who never checked out (walk-ups, comps).

Unlike ``mark_order_paid``, which settles an existing order, this creates
one from scratch. It skips every buyer-facing gate (draft status, paused or
not-yet-live sales, enabled payment methods) because it's a trusted staff
action, but still reserves stock with ``reserve_stock``. It then settles the
order through ``mark_order_paid``. Two audit entries result:
``order.create_manual`` and ``order.mark_paid``.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal
from app.models.enums import OrderStatus, PaymentMethod
from app.models.order import Order
from app.models.ticket import Ticket
from app.services.audit import record_audit_entry
from app.services.order_payment import mark_order_paid
from app.services.stock import InsufficientStockError, TicketTypeNotFoundError, reserve_stock


class ManualOrderError(Exception):
    """Base rejection; carries the HTTP status to return (like ``CheckoutError``)."""

    http_status: int = 422

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class TicketTypesNotFoundManualOrderError(ManualOrderError):
    """One or more requested ``ticket_type_id`` values don't exist."""

    http_status = 404

    def __init__(self) -> None:
        super().__init__("One or more ticket types were not found.")


class TicketTypeWrongShowManualOrderError(ManualOrderError):
    """A ticket type belongs to a different show."""

    http_status = 422

    def __init__(self) -> None:
        super().__init__("One or more ticket types do not belong to this show.")


class InsufficientStockManualOrderError(ManualOrderError):
    """A requested quantity exceeds a TicketType's real remaining stock."""

    http_status = 409

    def __init__(self, ticket_type_id: uuid.UUID, remaining: int) -> None:
        self.ticket_type_id = ticket_type_id
        self.remaining = remaining
        super().__init__(f"Only {max(remaining, 0)} of that ticket type remain available.")


@dataclass(frozen=True)
class ManualOrderItemInput:
    """One ticket type + quantity line."""

    ticket_type_id: uuid.UUID
    quantity: int


_PLACEHOLDER_EMAIL_DOMAIN = "no-email.invalid"
"""Used when the admin leaves the email blank: the column is NOT NULL, and
``.invalid`` is reserved as never deliverable. No email is sent in that case.
"""


def _placeholder_email() -> str:
    return f"walkin-{uuid.uuid4().hex[:12]}@{_PLACEHOLDER_EMAIL_DOMAIN}"


async def create_manual_order(
    session: AsyncSession,
    *,
    show_id: uuid.UUID,
    event_id: uuid.UUID,
    items: Sequence[ManualOrderItemInput],
    buyer_name: str,
    buyer_email: str | None,
    buyer_address: str | None,
    language: str,
    principal: Principal,
    method_label: str,
    reason: str | None,
) -> Order:
    """Create and immediately settle a ``manual`` order for ``show_id``.

    ``event_id`` comes from the caller's show lookup. Ticket types are verified
    to belong to ``show_id`` via the locked rows. Doesn't commit; raising
    :class:`ManualOrderError` leaves nothing behind.
    """
    quantities: dict[uuid.UUID, int] = {}
    for item in items:
        quantities[item.ticket_type_id] = quantities.get(item.ticket_type_id, 0) + item.quantity

    try:
        locked = await reserve_stock(session, quantities)
    except TicketTypeNotFoundError as exc:
        raise TicketTypesNotFoundManualOrderError() from exc
    except InsufficientStockError as exc:
        raise InsufficientStockManualOrderError(exc.ticket_type_id, exc.remaining) from exc

    if any(ticket_type.show_id != show_id for ticket_type in locked.values()):
        raise TicketTypeWrongShowManualOrderError()

    total: Decimal = sum((locked[tid].price * qty for tid, qty in quantities.items()), Decimal("0.00"))

    order = Order(
        event_id=event_id,
        buyer_name=buyer_name,
        buyer_email=buyer_email or _placeholder_email(),
        buyer_address=buyer_address or "",
        status=OrderStatus.PENDING,
        payment_method=PaymentMethod.MANUAL,
        total=total,
        language=language,
    )
    session.add(order)
    await session.flush()

    for ticket_type_id, qty in quantities.items():
        for _ in range(qty):
            session.add(Ticket(order_id=order.id, ticket_type_id=ticket_type_id))
    await session.flush()

    await record_audit_entry(
        session,
        principal,
        action="order.create_manual",
        target_type="Order",
        target_id=str(order.id),
        detail={
            "show_id": str(show_id),
            "buyer_name": buyer_name,
            "items": {str(tid): qty for tid, qty in quantities.items()},
        },
    )

    # Settle now: skipping online payment is the point. mark_order_paid
    # records the order.mark_paid audit entry with the admin's method/reason.
    await mark_order_paid(
        session, order_id=order.id, principal=principal, method_label=method_label, reason=reason
    )
    return order

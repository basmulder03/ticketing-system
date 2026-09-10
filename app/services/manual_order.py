"""Admin-created Orders that never went through buyer self-checkout at
all — the "offline" path per the user's NOTES: "For people without a
computer or phone, allow for an admin to create/do things with tickets,
like creating them for a show, e.g. without having the payment process
(of course with the correct audit logging)."

Distinct from the pre-existing manual mark-as-paid action
(``app.services.order_payment.mark_order_paid``), which settles an
ALREADY-EXISTING Order — one a buyer created themselves via self-checkout,
now ``pending``/``pending_door``, or a lapsed one being recovered. This
module creates the Order itself from nothing, for a buyer who never
submitted any checkout request in the first place (e.g. a walk-up buyer at
a box office with no computer or phone, or a comp ticket for a volunteer).

Deliberately bypasses every buyer-facing gate
``app.services.checkout.perform_checkout`` enforces (draft/publish status,
``sales_paused``, ``sales_live_at``, ``enabled_payment_methods``) — an
admin issuing a ticket by hand is a trusted staff action addressed
directly to one Show, not a public checkout request subject to sales
timing. Reuses ``app.services.stock.reserve_stock`` for the same
race-safe stock accounting every other order-creating path uses, and
``app.services.order_payment.mark_order_paid`` to settle the freshly
created Order to ``paid`` immediately — no new settlement logic, just a
new creation path feeding into the existing one. Two distinct audit
entries are written: ``order.create_manual`` (this module, documenting
that the Order didn't come through checkout at all, for which Show, and
what was requested) and ``order.mark_paid`` (written by
``mark_order_paid`` itself, documenting the settlement method/reason the
admin gave — e.g. "cash", "comp").
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
    """Base for every manual-order-creation rejection reason; carries the
    HTTP status the route should translate it to — same shape as
    ``app.services.checkout.CheckoutError``."""

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
    """A requested ``ticket_type_id`` exists but belongs to a different
    Show than the one this order is being created for."""

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
    """One requested ticket type + quantity line."""

    ticket_type_id: uuid.UUID
    quantity: int


_PLACEHOLDER_EMAIL_DOMAIN = "no-email.invalid"
"""Used only when the admin leaves ``buyer_email`` blank — a syntactically
valid-shaped but obviously-non-deliverable address, satisfying
``Order.buyer_email``'s ``nullable=False`` column without inventing a fake
real-looking address. ``.invalid`` is the IANA-reserved TLD for exactly
this purpose (RFC 2606) — nothing will ever attempt to deliver here, and
``app.services.manual_order.create_manual_order``'s caller (the API route)
skips confirmation-email dispatch entirely whenever the admin left the
email blank, so this value is never actually used to send anything."""


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
    """Create and immediately settle a ``manual``-method Order for
    ``show_id``, inside the caller's transaction.

    ``event_id`` is trusted from the caller (already resolved from
    ``show_id`` via a real DB lookup — see the route) rather than
    re-derived here, since :func:`~app.services.stock.reserve_stock`'s
    locked ``TicketType`` rows are what this function uses to verify every
    item actually belongs to ``show_id`` (not a second, redundant Show
    fetch): a mismatch there means at least one requested ticket type
    belongs to a different Show, never that ``event_id`` itself is wrong.

    Does not commit — the caller (the route) commits after this returns
    successfully, or rolls back (implicitly, by never committing) if a
    :class:`ManualOrderError` is raised.
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

    # Settle immediately — this whole feature exists specifically to skip
    # the online payment process, not to leave a second pending order type
    # around; `mark_order_paid` records its own `order.mark_paid` audit
    # entry with the admin-supplied method_label/reason (e.g. "cash").
    await mark_order_paid(
        session, order_id=order.id, principal=principal, method_label=method_label, reason=reason
    )
    return order

"""Checkout: validates a public buyer's order request against the
Event/Show/TicketType's draft & sales-timing state, then performs the
row-locked stock reservation and creates the ``Order`` + ``Ticket`` rows —
all inside one DB transaction. See ``app.services.stock`` for the row-
locking mechanics that make this race-safe under concurrent buyers.
"""

import hmac
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.enums import OrderStatus, PaymentMethod, PublishStatus
from app.models.event import Event
from app.models.order import Order
from app.models.show import Show
from app.models.ticket import Ticket
from app.models.ticket_type import TicketType
from app.services.stock import InsufficientStockError, TicketTypeNotFoundError, reserve_stock


class CheckoutError(Exception):
    """Base for every checkout-rejection reason; carries the HTTP status
    the route should translate it to. The route catches only this one base
    class (DRY) rather than every subclass individually — see
    ``app.api.routes.public.checkout``.
    """

    http_status: int = 422

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class TicketTypesNotFoundCheckoutError(CheckoutError):
    """One or more requested ``ticket_type_id`` values don't exist."""

    http_status = 404

    def __init__(self) -> None:
        super().__init__("One or more ticket types were not found.")


class MixedShowCheckoutError(CheckoutError):
    """The requested items span more than one Show — an Order can only
    span multiple TicketTypes within a single Show (see ``Order`` model
    docstring)."""

    def __init__(self) -> None:
        super().__init__("All items in one order must belong to the same show.")


class EventNotAvailableCheckoutError(CheckoutError):
    """The target Event or Show is still draft and no valid preview token
    was supplied — identical response to "doesn't exist" so a guess can't
    distinguish the two (see ``app.api.routes.public``)."""

    http_status = 404

    def __init__(self) -> None:
        super().__init__("This event is not currently available.")


class SalesNotLiveCheckoutError(CheckoutError):
    """The Event's ``EventConfig.sales_live_at`` is in the future."""

    http_status = 403

    def __init__(self) -> None:
        super().__init__("Sales are not live yet for this event.")


class SalesPausedCheckoutError(CheckoutError):
    """``Event.sales_paused`` is set (manual pause/resume override)."""

    http_status = 403

    def __init__(self) -> None:
        super().__init__("Sales are currently paused for this event.")


class PaymentMethodNotEnabledCheckoutError(CheckoutError):
    """The requested ``payment_method`` isn't in
    ``EventConfig.enabled_payment_methods`` for this event."""

    def __init__(self, method: PaymentMethod) -> None:
        super().__init__(f"Payment method '{method.value}' is not enabled for this event.")


class InsufficientStockCheckoutError(CheckoutError):
    """Not enough stock remained for one of the requested ticket types,
    determined under the row lock at reservation time."""

    http_status = 409

    def __init__(self, ticket_type_id: uuid.UUID, remaining: int) -> None:
        self.ticket_type_id = ticket_type_id
        self.remaining = remaining
        super().__init__(f"Only {remaining} ticket(s) remain for one of the requested ticket types.")


@dataclass(frozen=True)
class CheckoutItemInput:
    """One requested line item: a TicketType id and quantity."""

    ticket_type_id: uuid.UUID
    quantity: int


async def perform_checkout(
    session: AsyncSession,
    *,
    items: Sequence[CheckoutItemInput],
    buyer_name: str,
    buyer_email: str,
    buyer_address: str,
    language: str,
    payment_method: PaymentMethod,
    preview_token: str | None,
) -> Order:
    """Validate and execute one checkout, inside the caller's transaction.

    Validation order: ticket types exist -> all belong to one Show ->
    Event/Show are published (or a valid preview token was given, which
    bypasses this and the two checks below) -> sales not paused -> sales
    are live -> the chosen payment method is enabled -> (finally) enough
    stock remains, checked and reserved atomically via
    ``app.services.stock.reserve_stock``.

    Preview-mode note: a valid ``preview_token`` (matching the target
    Event's ``Event.preview_token``) bypasses the draft-publish gate per
    PROJECT_BRIEF.md's Draft & Preview section ("the full buyer journey can
    be reviewed before production credentials exist") — but ONLY while the
    Event or Show is actually still draft. Once BOTH are published, the
    token has no gating effect at all: ``sales_paused``/``sales_live_at``
    are unconditionally enforced regardless of any token presented. Without
    this restriction, a preview link handed to stakeholders pre-launch
    (explicitly meant to be shareable, per the brief) would remain a
    standing credential that permanently bypasses the manual "pause all
    sales now" incident kill-switch and the sales-embargo gate forever
    after the event goes live — defeating both, not just draft-gating.
    The token never bypasses the payment-method-enabled or
    stock-availability checks, since those are basic input validity, not
    publish-timing gates.

    Does not commit — the caller (the checkout route) commits after this
    returns successfully, or rolls back if a :class:`CheckoutError` is
    raised.
    """
    quantities: dict[uuid.UUID, int] = {}
    for item in items:
        quantities[item.ticket_type_id] = quantities.get(item.ticket_type_id, 0) + item.quantity

    result = await session.execute(
        select(TicketType)
        .where(TicketType.id.in_(quantities.keys()))
        .options(joinedload(TicketType.show).joinedload(Show.event).joinedload(Event.config))
    )
    ticket_types = list(result.scalars().unique().all())
    if len(ticket_types) != len(quantities):
        raise TicketTypesNotFoundCheckoutError()

    show_ids = {tt.show_id for tt in ticket_types}
    if len(show_ids) != 1:
        raise MixedShowCheckoutError()

    show = ticket_types[0].show
    event = show.event
    config = event.config

    is_draft = event.status != PublishStatus.PUBLISHED or show.status != PublishStatus.PUBLISHED
    has_valid_preview_token = preview_token is not None and hmac.compare_digest(preview_token, event.preview_token)
    if is_draft:
        if not has_valid_preview_token:
            raise EventNotAvailableCheckoutError()
        # Draft + valid token: reviewable pre-launch, per the brief — sales
        # timing gates below don't apply to something that isn't live yet.
    else:
        # Fully published: sales_paused/sales_live_at are unconditionally
        # enforced. A preview token (even a correct one) grants no bypass
        # here — see this function's docstring for why.
        if event.sales_paused:
            raise SalesPausedCheckoutError()
        sales_live_at = config.sales_live_at if config is not None else None
        if sales_live_at is not None and datetime.now(UTC) < sales_live_at:
            raise SalesNotLiveCheckoutError()

    enabled_methods = config.enabled_payment_methods if config is not None else []
    if payment_method not in enabled_methods:
        raise PaymentMethodNotEnabledCheckoutError(payment_method)

    try:
        locked = await reserve_stock(session, quantities)
    except TicketTypeNotFoundError as exc:
        raise TicketTypesNotFoundCheckoutError() from exc
    except InsufficientStockError as exc:
        raise InsufficientStockCheckoutError(exc.ticket_type_id, exc.remaining) from exc

    total: Decimal = sum((locked[tid].price * qty for tid, qty in quantities.items()), Decimal("0.00"))

    order = Order(
        event_id=event.id,
        buyer_name=buyer_name,
        buyer_email=buyer_email,
        buyer_address=buyer_address,
        status=OrderStatus.PENDING,
        payment_method=payment_method,
        total=total,
        language=language,
    )
    session.add(order)
    await session.flush()

    for ticket_type_id, qty in quantities.items():
        for _ in range(qty):
            session.add(Ticket(order_id=order.id, ticket_type_id=ticket_type_id))
    await session.flush()

    return order

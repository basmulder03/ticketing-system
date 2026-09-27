"""Public checkout: validate the request against draft and sales-timing state,
reserve stock under row locks, create the Order and Tickets, and start
payment for the chosen method — all in one transaction.
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

from app.core.config import get_settings
from app.models.enums import OrderStatus, PaymentMethod, PublishStatus
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.order import Order
from app.models.show import Show
from app.models.ticket import Ticket
from app.models.ticket_type import TicketType
from app.services.invoicing import issue_invoice_for_order
from app.services.mollie import MollieApiError, create_mollie_payment, resolve_mollie_api_key
from app.services.order_payment import SYSTEM_PRINCIPAL, mark_order_paid
from app.services.stock import InsufficientStockError, TicketTypeNotFoundError, reserve_stock
from app.services.ticket_delivery import sign_order_tickets


class CheckoutError(Exception):
    """Base rejection; carries the HTTP status and a stable machine-readable
    ``error_code`` (the route catches only this base class and exposes both).
    """

    http_status: int = 422
    error_code: str = "checkout_failed"

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class TicketTypesNotFoundCheckoutError(CheckoutError):
    """One or more requested ``ticket_type_id`` values don't exist."""

    http_status = 404
    error_code = "ticket_types_not_found"

    def __init__(self) -> None:
        super().__init__("One or more ticket types were not found.")


class MixedShowCheckoutError(CheckoutError):
    """Items span more than one show (an order is for a single show)."""

    error_code = "mixed_show"

    def __init__(self) -> None:
        super().__init__("All items in one order must belong to the same show.")


class EventNotAvailableCheckoutError(CheckoutError):
    """Draft event/show without a valid preview token — same response as "not
    found", so guesses can't tell them apart.
    """

    http_status = 404
    error_code = "event_not_available"

    def __init__(self) -> None:
        super().__init__("This event is not currently available.")


class SalesNotLiveCheckoutError(CheckoutError):
    """The Event's ``EventConfig.sales_live_at`` is in the future."""

    http_status = 403
    error_code = "sales_not_live"

    def __init__(self) -> None:
        super().__init__("Sales are not live yet for this event.")


class SalesPausedCheckoutError(CheckoutError):
    """``Event.sales_paused`` is set."""

    http_status = 403
    error_code = "sales_paused"

    def __init__(self) -> None:
        super().__init__("Sales are currently paused for this event.")


class PaymentMethodNotEnabledCheckoutError(CheckoutError):
    """The method isn't enabled for this event."""

    error_code = "payment_method_not_enabled"

    def __init__(self, method: PaymentMethod) -> None:
        super().__init__(f"Payment method '{method.value}' is not enabled for this event.")


class InsufficientStockCheckoutError(CheckoutError):
    """Not enough stock, determined under the row lock."""

    http_status = 409
    error_code = "insufficient_stock"

    def __init__(self, ticket_type_id: uuid.UUID, remaining: int) -> None:
        self.ticket_type_id = ticket_type_id
        self.remaining = remaining
        super().__init__(f"Only {remaining} ticket(s) remain for one of the requested ticket types.")


class PaymentInitiationCheckoutError(CheckoutError):
    """Couldn't start a Mollie payment: the call failed, or no key is configured
    outside the preview sandbox. The route rolls everything back, including the
    stock reservation.
    """

    http_status = 502
    error_code = "payment_initiation_failed"

    def __init__(self) -> None:
        super().__init__("Could not initiate payment with Mollie. Please try again shortly.")


@dataclass(frozen=True)
class CheckoutItemInput:
    """One ticket type + quantity line."""

    ticket_type_id: uuid.UUID
    quantity: int


@dataclass(frozen=True)
class PaymentInitiationResult:
    """What each payment method's initiation function returns. A new provider
    is a new function returning this plus one ``elif`` in :func:`perform_checkout`.

    ``redirect_url``: the payment page to send the buyer to (Mollie, the demo
    page), or ``None``. ``simulated_payment``: the order is already paid.
    """

    redirect_url: str | None
    simulated_payment: bool = False


@dataclass(frozen=True)
class CheckoutResult:
    """The created order, plus the payment-page URL when there is one."""

    order: Order
    payment_redirect_url: str | None
    simulated_payment: bool = False
    """``True`` only for the preview sandbox path: the order is already paid, so
    the route sends the confirmation email after committing. ``False`` for real
    Mollie, demo and door orders, which settle later.
    """


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
) -> CheckoutResult:
    """Validate and execute one checkout inside the caller's transaction.

    Checks, in order: ticket types exist → one show → published (or valid
    preview token) → not paused → sales live → method enabled → stock (reserved
    atomically via ``reserve_stock``).

    A preview token bypasses only the *draft* gate, and only while the event or
    show is still draft. Once published it grants nothing, so a shared preview
    link can never bypass the sales pause or embargo.

    ``door`` orders start ``PENDING_DOOR`` with no payment step. Doesn't commit;
    on :class:`CheckoutError` the caller rolls back.
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
        # Draft + valid token: reviewable pre-launch; sales timing doesn't apply.
    else:
        # Published: pause and sales timing always apply, token or not.
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

    # Door orders hold stock as PENDING_DOOR until marked paid; every other
    # method starts PENDING.
    initial_status = OrderStatus.PENDING_DOOR if payment_method == PaymentMethod.DOOR else OrderStatus.PENDING
    order = Order(
        event_id=event.id,
        buyer_name=buyer_name,
        buyer_email=buyer_email,
        buyer_address=buyer_address,
        status=initial_status,
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

    # Plain if/elif rather than a registry: with so few methods a registry is
    # indirection for nothing. PaymentInitiationResult is what keeps it extensible.
    initiation = PaymentInitiationResult(redirect_url=None)
    if payment_method == PaymentMethod.MOLLIE:
        # Sandbox only for genuine draft previews — a preview token stays valid
        # forever, so it must never skip real payment on a published event.
        initiation = await _initiate_mollie_payment(
            session,
            order=order,
            config=config,
            preview_sandbox_allowed=is_draft and has_valid_preview_token,
            is_preview_checkout=has_valid_preview_token,
        )
    elif payment_method == PaymentMethod.DEMO:
        initiation = _initiate_demo_payment(order=order)

    return CheckoutResult(
        order=order, payment_redirect_url=initiation.redirect_url, simulated_payment=initiation.simulated_payment
    )


async def _initiate_mollie_payment(
    session: AsyncSession,
    *,
    order: Order,
    config: EventConfig | None,
    preview_sandbox_allowed: bool,
    is_preview_checkout: bool,
) -> PaymentInitiationResult:
    """Start payment for a just-created ``mollie`` order.

    1. A key is configured for the event's mode: create a Mollie payment and
       return its checkout URL (test mode is Mollie's own sandbox).
    2. No key, but this is a draft preview checkout: simulate — mark the order
       paid as ``SYSTEM_PRINCIPAL``, sign tickets and issue the invoice in this
       transaction, and return ``simulated_payment=True`` so the route emails
       after commit. No charge, no external call.
    3. No key on a real checkout: operator error — raise
       :class:`PaymentInitiationCheckoutError` rather than fake a payment.

    A failed Mollie call also raises, rolling back the whole checkout.
    """
    api_key = resolve_mollie_api_key(config)
    if api_key is None:
        if not preview_sandbox_allowed:
            raise PaymentInitiationCheckoutError()
        await mark_order_paid(
            session,
            order_id=order.id,
            principal=SYSTEM_PRINCIPAL,
            method_label="mollie_simulated",
            reason=(
                "Simulated preview-mode checkout: no Mollie API key is configured yet for this "
                "event, so no real Mollie payment or charge was created."
            ),
        )
        await sign_order_tickets(session, order=order)
        # Issue the invoice in this transaction too, like the webhook's paid branch.
        await issue_invoice_for_order(session, order=order, principal=SYSTEM_PRINCIPAL)
        return PaymentInitiationResult(redirect_url=None, simulated_payment=True)

    settings = get_settings()
    base_url = settings.public_base_url.rstrip("/")
    redirect_url = f"{base_url}/order-confirmation/{order.id}"
    if is_preview_checkout:
        # Cosmetic only (which confirmation template renders): mirrors the web
        # layer's "a preview token was used" rule, not a security boundary.
        redirect_url += "?preview=1"
    webhook_url = f"{base_url}/api/v1/public/mollie-webhook"

    try:
        created = await create_mollie_payment(
            api_key=api_key,
            order_id=order.id,
            amount=order.total,
            redirect_url=redirect_url,
            webhook_url=webhook_url,
            description=f"Order {order.id}",
        )
    except MollieApiError as exc:
        raise PaymentInitiationCheckoutError() from exc

    order.mollie_payment_id = created.payment_id
    # Pin the mode so the webhook reconciles with this key even if an admin
    # switches test/live while the order is pending.
    order.mollie_mode = config.mollie_mode if config is not None else None
    await session.flush()
    return PaymentInitiationResult(redirect_url=created.checkout_url, simulated_payment=False)


def _initiate_demo_payment(*, order: Order) -> PaymentInitiationResult:
    """Start payment for a just-created ``demo`` order: just point the buyer at the
    app's own ``/demo-payment/<id>`` page. The order stays ``PENDING`` until they
    choose "success" or "failure" there — the same pending-then-settled shape as
    a real provider (unlike the preview sandbox, which settles instantly).
    """
    settings = get_settings()
    base_url = settings.public_base_url.rstrip("/")
    return PaymentInitiationResult(redirect_url=f"{base_url}/demo-payment/{order.id}", simulated_payment=False)

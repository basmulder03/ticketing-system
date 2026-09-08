"""Checkout: validates a public buyer's order request against the
Event/Show/TicketType's draft & sales-timing state, then performs the
row-locked stock reservation and creates the ``Order`` + ``Ticket`` rows,
and (Milestone 3) initiates payment for a ``mollie``-method order — all
inside one DB transaction. See ``app.services.stock`` for the row-locking
mechanics that make stock reservation race-safe under concurrent buyers.
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


class PaymentInitiationCheckoutError(CheckoutError):
    """Initiating payment for a ``mollie``-method order failed: either a
    real Mollie call was attempted (a key IS configured for this event's
    current ``EventConfig.mollie_mode``) and Mollie's API rejected it or
    couldn't be reached, or no key is configured at all and this isn't the
    preview-token sandbox case where that's allowed (see
    ``_initiate_mollie_payment``). The caller (the checkout route) rolls
    back the whole transaction on this — including the stock reservation —
    since a payment that can't even be created shouldn't hold stock.
    """

    http_status = 502

    def __init__(self) -> None:
        super().__init__("Could not initiate payment with Mollie. Please try again shortly.")


@dataclass(frozen=True)
class CheckoutItemInput:
    """One requested line item: a TicketType id and quantity."""

    ticket_type_id: uuid.UUID
    quantity: int


@dataclass(frozen=True)
class CheckoutResult:
    """What :func:`perform_checkout` returns: the created ``Order`` plus,
    for a ``mollie``-method order that just created a real Mollie payment,
    the Mollie-hosted checkout URL to redirect the buyer to."""

    order: Order
    mollie_checkout_url: str | None
    simulated_payment: bool = False
    """``True`` only when this checkout took the preview-mode simulated-
    payment path (see :func:`_initiate_mollie_payment` case 2) — the Order
    is already ``paid`` by the time this is returned, with no real Mollie
    payment involved. The caller (``app.api.routes.public.checkout``) uses
    this, once its own transaction has committed, as the signal to trigger
    the Milestone 4 order-confirmation email dispatch for this genuinely-
    just-paid Order — see ``app.services.ticket_delivery.
    send_order_confirmation_email``. Always ``False`` for a real Mollie
    payment (still ``pending`` until the webhook confirms it later) and for
    a ``door`` order (settled in a future milestone)."""


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

    After the Order/Tickets are created, a ``mollie``-method order also has
    its payment initiated (real Mollie call, or the preview-sandbox
    simulated-paid path — see :func:`_initiate_mollie_payment`); a ``door``
    order skips this entirely and stays ``PENDING`` (Milestone 6 territory).

    Does not commit — the caller (the checkout route) commits after this
    returns successfully, or rolls back if a :class:`CheckoutError` is
    raised (including :class:`PaymentInitiationCheckoutError`, which rolls
    back the stock reservation too).
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

    mollie_checkout_url: str | None = None
    simulated_payment = False
    if payment_method == PaymentMethod.MOLLIE:
        # Sandbox eligibility mirrors the draft-gate bypass above exactly
        # (``is_draft and has_valid_preview_token``), not "any checkout that
        # happens to carry a valid preview token" — a preview token remains
        # valid forever (see ``Event.preview_token`` docstring) and, per
        # this function's own docstring, grants no bypass at all once the
        # Event/Show is published. Scoping the simulated-payment path the
        # same way means it can never be used to skip real payment
        # processing on a live, published event.
        mollie_checkout_url, simulated_payment = await _initiate_mollie_payment(
            session,
            order=order,
            config=config,
            preview_sandbox_allowed=is_draft and has_valid_preview_token,
            is_preview_checkout=has_valid_preview_token,
        )

    return CheckoutResult(order=order, mollie_checkout_url=mollie_checkout_url, simulated_payment=simulated_payment)


async def _initiate_mollie_payment(
    session: AsyncSession,
    *,
    order: Order,
    config: EventConfig | None,
    preview_sandbox_allowed: bool,
    is_preview_checkout: bool,
) -> tuple[str | None, bool]:
    """Kick off payment for a ``mollie``-method Order that was just created
    (still ``PENDING``, already flushed). Returns
    ``(mollie_checkout_url, simulated_payment)`` — see
    :class:`CheckoutResult`'s ``simulated_payment`` field.

    Three cases:

    1. A real Mollie API key is configured for this event's current
       ``EventConfig.mollie_mode`` — call Mollie's Create Payment API
       (``app.services.mollie.create_mollie_payment``) and return the
       Mollie-hosted checkout URL for the buyer to be redirected to.
       Mollie's own test mode (when ``mollie_mode == TEST``) is already a
       safe sandbox; nothing extra is needed for that case per
       PROJECT_BRIEF.md's Draft & Preview section. ``simulated_payment`` is
       ``False``.
    2. No key is configured at all, but ``preview_sandbox_allowed`` is
       True (a genuine draft/preview-token checkout) — skip Mollie
       entirely and simulate: immediately mark the order ``paid`` via
       :func:`app.services.order_payment.mark_order_paid`, attributed to
       the automated ``SYSTEM_PRINCIPAL`` with a reason that makes the
       simulated nature explicit in the audit log, and sign its Tickets'
       QR tokens (``app.services.ticket_delivery.sign_order_tickets`` —
       Milestone 4) and issue its Invoice (``app.services.invoicing.
       issue_invoice_for_order`` — Milestone 5) inside this same
       transaction. This is this project's
       chosen mechanism for PROJECT_BRIEF.md's "clearly-labeled 'test
       checkout'" requirement — no real charge, no external call at all.
       Returns ``(None, True)`` (nothing to redirect to; the caller/route
       sends the buyer straight to order-confirmation, same as a ``door``
       order) — the caller uses the ``True`` flag to trigger the
       order-confirmation EMAIL dispatch itself, AFTER its own transaction
       commits (email sending is a separate, best-effort step that must
       never be inside the same transaction as the payment-status/stock
       changes — see ``app.services.ticket_delivery`` module docstring).
       In dev this naturally routes through the local Mailpit SMTP sink,
       same as any other event's configured SMTP settings.
    3. No key is configured and ``preview_sandbox_allowed`` is False (a
       real, non-preview checkout with Mollie misconfigured) — this is a
       genuine operator error, not something to silently paper over with a
       simulated payment for a real buyer. Raises
       :class:`PaymentInitiationCheckoutError`.

    Raises :class:`PaymentInitiationCheckoutError` if a real Mollie call
    (case 1) fails (network/API error) — the caller rolls back the whole
    checkout, including its stock reservation.
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
        # Milestone 5: issue the Invoice inside this same transaction too,
        # exactly like the Mollie webhook's equivalent fresh-payment branch
        # — see app.services.invoicing module docstring.
        await issue_invoice_for_order(session, order=order, principal=SYSTEM_PRINCIPAL)
        return None, True

    settings = get_settings()
    base_url = settings.public_base_url.rstrip("/")
    redirect_url = f"{base_url}/order-confirmation/{order.id}"
    if is_preview_checkout:
        # Matches ``app.web.routes.public_site``'s own ``is_preview =
        # token is not None`` rule for the confirmation page (not scoped to
        # ``preview_sandbox_allowed``/draft-only) — this query param is
        # purely cosmetic (which template variant renders), not a security
        # boundary, so it should reflect "a preview token was used" exactly
        # like the rest of the web layer already does.
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
    # Snapshot which mode's key was actually used, so the webhook
    # reconciles with THIS key even if an admin flips EventConfig.mollie_mode
    # while this Order is still pending — see Order.mollie_mode's docstring.
    order.mollie_mode = config.mollie_mode if config is not None else None
    await session.flush()
    return created.checkout_url, False

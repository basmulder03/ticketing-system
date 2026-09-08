"""Public, unauthenticated read routes for the landing-page/show/
ticket-type data `frontend-theming` renders (Milestone 2), the public
checkout endpoint that creates a ``pending`` Order and initiates its
payment (Milestone 3), and the Mollie payment-status webhook.

Every route here is intentionally unauthenticated by design (this is the
public buyer-facing site, and Mollie's webhook caller has no admin session
or agent API key to present) — the access control that matters is
draft-vs-published + the unguessable preview token (see
``app.models.event.Event.preview_token``) for the read/checkout routes, and
"fetch the authoritative status back from Mollie's own API, never trust the
webhook body" for the webhook (see ``app.services.mollie`` module
docstring).
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.rate_limit import (
    checkout_rate_limiter,
    mollie_webhook_rate_limiter,
    rate_limit_dependency,
)
from app.db.session import get_session
from app.models.enums import OrderStatus, PublishStatus
from app.models.event import Event
from app.models.order import Order
from app.models.show import Show
from app.models.ticket import Ticket
from app.schemas.order import CheckoutRequest, OrderOut, TicketOut
from app.schemas.public import PublicEventOut, PublicShowOut, PublicThemeOut, PublicTicketTypeOut
from app.services.checkout import CheckoutError, CheckoutItemInput, CheckoutResult, perform_checkout
from app.services.mollie import MollieApiError, fetch_mollie_payment_status, resolve_mollie_api_key
from app.services.order_payment import SYSTEM_PRINCIPAL, mark_order_paid, release_order_stock
from app.services.stock import attach_remaining
from app.services.theme_images import public_url_for

router = APIRouter(prefix="/api/v1/public", tags=["public"])

# Eagerly load everything a landing-page response needs in one round trip:
# the Theme, the EventConfig (for sales_live_at/enabled_payment_methods —
# never the secret fields, those are simply never read here), and every
# Show with its TicketTypes.
_EVENT_LOAD_OPTIONS = (
    selectinload(Event.theme),
    selectinload(Event.config),
    selectinload(Event.shows).selectinload(Show.ticket_types),
)


async def _get_event_by_slug(session: AsyncSession, slug: str) -> Event | None:
    result = await session.execute(select(Event).where(Event.slug == slug).options(*_EVENT_LOAD_OPTIONS))
    return result.scalar_one_or_none()


async def _get_event_by_preview_token(session: AsyncSession, token: str) -> Event | None:
    result = await session.execute(select(Event).where(Event.preview_token == token).options(*_EVENT_LOAD_OPTIONS))
    return result.scalar_one_or_none()


async def _build_public_event_out(
    session: AsyncSession, event: Event, *, published_only: bool, is_preview: bool
) -> PublicEventOut:
    """Assemble the full nested response for one Event.

    When ``published_only`` is True, Shows with a non-published ``status``
    are dropped (the plain published-slug route); the preview-token route
    passes ``published_only=False`` so every Show is included regardless of
    its own draft/published state, per PROJECT_BRIEF.md's Draft & Preview
    section.
    """
    shows = [s for s in event.shows if not published_only or s.status == PublishStatus.PUBLISHED]
    all_ticket_types = [tt for show in shows for tt in show.ticket_types]
    await attach_remaining(session, all_ticket_types)

    theme_out: PublicThemeOut | None = None
    if event.theme is not None:
        theme_out = PublicThemeOut(
            primary_color=event.theme.primary_color,
            secondary_color=event.theme.secondary_color,
            accent_color=event.theme.accent_color,
            font_choice=event.theme.font_choice,
            logo_url=public_url_for(event.theme.logo_path),
            background_image_url=public_url_for(event.theme.background_image_path),
            custom_css=event.theme.custom_css,
        )

    show_outs = [
        PublicShowOut(
            id=str(show.id),
            date=show.date,
            doors_time=show.doors_time,
            start_time=show.start_time,
            venue_name=show.venue_name,
            venue_address=show.venue_address,
            capacity=show.capacity,
            status=show.status,
            ticket_types=[
                PublicTicketTypeOut(
                    id=str(tt.id),
                    name=tt.name,
                    price=tt.price,
                    service_fee_included=tt.service_fee_included,
                    quantity_available=tt.quantity_available,
                    remaining=tt.remaining,
                )
                for tt in sorted(show.ticket_types, key=lambda t: t.created_at)
            ],
        )
        for show in sorted(shows, key=lambda s: s.date)
    ]

    config = event.config
    return PublicEventOut(
        id=str(event.id),
        name=event.name,
        slug=event.slug,
        description=event.description,
        status=event.status,
        sales_paused=event.sales_paused,
        sales_live_at=config.sales_live_at if config is not None else None,
        enabled_payment_methods=config.enabled_payment_methods if config is not None else [],
        theme=theme_out,
        shows=show_outs,
        is_preview=is_preview,
    )


@router.get("/events/{slug}")
async def get_public_event(slug: str, session: AsyncSession = Depends(get_session)) -> PublicEventOut:
    """Fetch a published Event by its public slug, with its published
    Shows/TicketTypes and Theme nested — everything `frontend-theming`
    needs to render the landing page in one call.

    404s for a draft Event, and for a slug that doesn't exist at all, with
    the same response either way — so a plain published-URL guess can
    never distinguish "no such event" from "exists but still draft" (see
    PROJECT_BRIEF.md's Draft & Preview: a draft is only reachable via its
    unguessable preview token, never the normal slug URL).
    """
    event = await _get_event_by_slug(session, slug)
    if event is None or event.status != PublishStatus.PUBLISHED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found.")
    return await _build_public_event_out(session, event, published_only=True, is_preview=False)


@router.get("/preview/{token}")
async def get_preview_event(token: str, session: AsyncSession = Depends(get_session)) -> PublicEventOut:
    """Fetch an Event (draft or published) by its unguessable preview
    token — includes every Show regardless of its own draft/published
    status, so a stakeholder reviewing a draft event sees the full picture
    before anything goes live.
    """
    event = await _get_event_by_preview_token(session, token)
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Preview not found.")
    return await _build_public_event_out(session, event, published_only=False, is_preview=True)


def _parse_ticket_type_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="One or more ticket types were not found."
        ) from None


async def _order_to_out(session: AsyncSession, order: Order, *, mollie_checkout_url: str | None = None) -> OrderOut:
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
        mollie_checkout_url=mollie_checkout_url,
    )


@router.post(
    "/checkout",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit_dependency(checkout_rate_limiter))],
)
async def checkout(
    body: CheckoutRequest,
    session: AsyncSession = Depends(get_session),
) -> OrderOut:
    """Create a ``pending`` Order (and its Ticket rows) for a public buyer,
    and (Milestone 3) initiate payment for it.

    This endpoint performs the row-locked stock check/reservation (see
    ``app.services.stock.reserve_stock``), persists the Order, and — for
    ``payment_method=mollie`` — creates the Mollie payment (or resolves the
    preview-mode simulated-payment path) via
    ``app.services.checkout.perform_checkout``; see that function's
    docstring for the full flow. Rate-limited per client IP (see
    ``app.core.rate_limit.checkout_rate_limiter``) per the brief's "rate
    limiting on checkout... endpoints" requirement.

    The response's ``mollie_checkout_url`` is set whenever a real Mollie
    payment was just created — the caller (``app.web.routes.public_site``)
    must redirect the buyer there instead of straight to order-confirmation.

    On any validation/availability/stock/payment-initiation failure, the
    transaction is rolled back and a specific 403/404/409/422/502 is
    returned (see ``app.services.checkout.CheckoutError`` and its
    subclasses) — never an unhandled 500 for an expected rejection reason.
    """
    items = [
        CheckoutItemInput(ticket_type_id=_parse_ticket_type_id(item.ticket_type_id), quantity=item.quantity)
        for item in body.items
    ]
    try:
        result: CheckoutResult = await perform_checkout(
            session,
            items=items,
            buyer_name=body.buyer_name,
            buyer_email=body.buyer_email,
            buyer_address=body.buyer_address,
            language=body.language,
            payment_method=body.payment_method,
            preview_token=body.preview_token,
        )
    except CheckoutError as exc:
        await session.rollback()
        raise HTTPException(status_code=exc.http_status, detail=exc.detail) from exc

    await session.commit()
    return await _order_to_out(session, result.order, mollie_checkout_url=result.mollie_checkout_url)


_MOLLIE_FAILURE_STATUSES: dict[str, OrderStatus] = {
    "expired": OrderStatus.EXPIRED,
    "failed": OrderStatus.CANCELLED,
    "canceled": OrderStatus.CANCELLED,
}
"""Maps a terminal-failure Mollie payment status to the ``OrderStatus`` it
transitions a still-``PENDING`` Order to (see
``app.services.order_payment.release_order_stock``). ``expired`` maps to
``OrderStatus.EXPIRED`` (name match); Mollie's ``failed``/``canceled`` both
map to ``OrderStatus.CANCELLED`` — ``OrderStatus`` has no separate "failed"
value and "the payment failed" and "the buyer/Mollie canceled it" are the
same outcome for stock-release purposes (see ``app.models.enums.OrderStatus``).
Every status absent from this dict (``open``, ``pending``, ``authorized``)
means the payment is still in progress — no Order transition happens for
those; the webhook will fire again once Mollie reaches a terminal state."""


@router.post(
    "/mollie-webhook",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(rate_limit_dependency(mollie_webhook_rate_limiter))],
)
async def mollie_webhook(request: Request, session: AsyncSession = Depends(get_session)) -> Response:
    """Mollie payment status webhook — public and unauthenticated by
    necessity (Mollie's servers call this directly, with no admin session
    or agent API key available to present).

    **This does NOT verify an HMAC/signature on the request body** — Mollie
    doesn't send one. Mollie's webhook body is only ever a form-encoded
    ``id=tr_xxx``; that id is treated purely as a lookup key, never as a
    trusted statement of payment status. The actual, authoritative status
    is fetched fresh from Mollie's own API (``GET /v2/payments/{id}``,
    using the Order's own Event's configured API key — see
    ``app.services.mollie`` module docstring for the full reasoning) and
    THAT response is what gets reconciled. This is the real, secure Mollie
    integration pattern, not a simplification.

    Idempotent: reconciling the same payment id twice (Mollie retries any
    delivery that doesn't get a fast 200, and the buyer's browser may also
    reload the redirect-back page) is always a safe no-op the second time —
    see ``app.services.order_payment.mark_order_paid`` /
    ``release_order_stock``, both of which row-lock the Order and only ever
    transition it once out of ``PENDING``.

    Always responds quickly. An unknown payment id (no matching Order — a
    stale/foreign webhook) and an in-progress Mollie status (``open``/
    ``pending``/``authorized`` — nothing to reconcile yet) both return 200
    so Mollie doesn't keep retrying something that will never change here.
    A genuine transient failure (Mollie unreachable, or this event's Mollie
    key can't be resolved) returns a 502 so Mollie's own retry mechanism
    tries again later, rather than silently swallowing a real payment
    confirmation.
    """
    form = await request.form()
    raw_payment_id = form.get("id")
    if not isinstance(raw_payment_id, str) or not raw_payment_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing payment id.")

    result = await session.execute(select(Order).where(Order.mollie_payment_id == raw_payment_id))
    order = result.scalar_one_or_none()
    if order is None:
        # Nothing this app knows about — 200 so Mollie stops retrying.
        return Response(status_code=status.HTTP_200_OK)

    event = await session.get(Event, order.event_id, options=[selectinload(Event.config)])
    config = event.config if event is not None else None
    api_key = resolve_mollie_api_key(config)
    if api_key is None:
        # Should not normally happen (this Order's payment was created with
        # a key in the first place) — but if the key/mode was cleared out
        # from under an in-flight payment, fail loudly (502) rather than
        # silently dropping a payment confirmation.
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Mollie is not configured for this event.")

    try:
        mollie_status = await fetch_mollie_payment_status(api_key=api_key, payment_id=raw_payment_id)
    except MollieApiError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Could not verify payment status with Mollie."
        ) from None

    if mollie_status == "paid":
        await mark_order_paid(
            session,
            order_id=order.id,
            principal=SYSTEM_PRINCIPAL,
            method_label="mollie",
            reason="Confirmed via Mollie webhook reconciliation.",
        )
    elif mollie_status in _MOLLIE_FAILURE_STATUSES:
        await release_order_stock(
            session,
            order_id=order.id,
            new_status=_MOLLIE_FAILURE_STATUSES[mollie_status],
            principal=SYSTEM_PRINCIPAL,
            reason=f"Mollie payment status: {mollie_status}.",
        )
    # else: still in progress (open/pending/authorized) — nothing to do yet.

    await session.commit()
    return Response(status_code=status.HTTP_200_OK)

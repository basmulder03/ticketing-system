"""Public, unauthenticated routes: event/homepage reads, checkout, demo
payments, and the Mollie webhook.

Access control is draft-vs-published plus the unguessable preview token; the
webhook never trusts its body and re-fetches status from Mollie.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.routes._utils import parse_uuid_or_404
from app.core.rate_limit import (
    checkout_rate_limiter,
    mollie_webhook_rate_limiter,
    rate_limit_dependency,
)
from app.db.session import get_session
from app.models.enums import OrderStatus, PaymentMethod, PublishStatus
from app.models.event import Event
from app.models.order import Order
from app.models.show import Show
from app.models.ticket import Ticket
from app.models.ticket_type import TicketType
from app.schemas.demo_payment import DemoPaymentItemOut, DemoPaymentOut
from app.schemas.order import CheckoutRequest, OrderOut, TicketOut
from app.schemas.public import (
    PublicEventOut,
    PublicEventSummaryOut,
    PublicHomepageOut,
    PublicShowOut,
    PublicThemeOut,
    PublicTicketTypeOut,
)
from app.services.audit import record_audit_entry
from app.services.checkout import CheckoutError, CheckoutItemInput, CheckoutResult, perform_checkout
from app.services.door_reservation_email import send_door_payment_confirmation_email
from app.services.invoicing import issue_invoice_for_order
from app.services.mollie import MollieApiError, fetch_mollie_payment_status, resolve_mollie_api_key
from app.services.order_payment import SYSTEM_PRINCIPAL, mark_order_paid, release_order_stock
from app.services.stock import InsufficientStockError, TicketTypeNotFoundError, attach_remaining
from app.services.theme_images import public_url_for
from app.services.ticket_delivery import send_order_confirmation_email, sign_order_tickets

router = APIRouter(prefix="/api/v1/public", tags=["public"])


class CheckoutHTTPException(HTTPException):
    """An :class:`~app.services.checkout.CheckoutError` translated to HTTP.
    Carries ``error_code`` alongside the usual string ``detail``, so a client
    can branch on the stable code instead of matching English text (see
    ``app/main.py``'s handler for the response shape).
    """

    def __init__(self, *, status_code: int, detail: str, error_code: str) -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.error_code = error_code


# One round trip: theme, config (non-secret fields only), shows and ticket types.
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
    """The full nested response for one event.

    ``published_only`` drops draft shows (the preview route keeps them). Shows
    without ticket types are kept: the beamer countdown still needs them; the
    landing page's ticket picker filters them itself.
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


@router.get("/homepage")
async def get_public_homepage(session: AsyncSession = Depends(get_session)) -> PublicHomepageOut:
    """Homepage data: every published event (never drafts), plus the default
    event's slug when that event is published.
    """
    result = await session.execute(
        select(Event).where(Event.status == PublishStatus.PUBLISHED).order_by(Event.created_at.desc())
    )
    published_events = result.scalars().all()
    default_slug = next((e.slug for e in published_events if e.is_default_event), None)
    return PublicHomepageOut(
        default_event_slug=default_slug,
        events=[
            PublicEventSummaryOut(name=e.name, slug=e.slug, description=e.description)
            for e in published_events
        ],
    )


@router.get("/events/{slug}")
async def get_public_event(slug: str, session: AsyncSession = Depends(get_session)) -> PublicEventOut:
    """A published event by slug. Drafts and unknown slugs get the same 404, so a
    guess can't reveal that a draft exists.
    """
    event = await _get_event_by_slug(session, slug)
    if event is None or event.status != PublishStatus.PUBLISHED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found.")
    return await _build_public_event_out(session, event, published_only=True, is_preview=False)


@router.get("/preview/{token}")
async def get_preview_event(token: str, session: AsyncSession = Depends(get_session)) -> PublicEventOut:
    """An event (draft or published) by preview token, with all its shows."""
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


async def _order_to_out(session: AsyncSession, order: Order, *, payment_redirect_url: str | None = None) -> OrderOut:
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
        payment_redirect_url=payment_redirect_url,
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
    """Create an order and start its payment (see ``perform_checkout``).
    Rate-limited per IP.

    Expected rejections roll back and return a specific 4xx/502, never a 500.
    ``payment_redirect_url`` means "send the buyer there first" (Mollie or the
    demo page). After commit, best-effort: a sandbox-paid order gets its
    confirmation email, and a door order gets its reservation email.
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
        raise CheckoutHTTPException(
            status_code=exc.http_status, detail=exc.detail, error_code=exc.error_code
        ) from exc

    await session.commit()
    if result.simulated_payment:
        await send_order_confirmation_email(session, order_id=result.order.id, principal=SYSTEM_PRINCIPAL)
    elif result.order.payment_method == PaymentMethod.DOOR:
        await send_door_payment_confirmation_email(session, order_id=result.order.id, principal=SYSTEM_PRINCIPAL)
    return await _order_to_out(session, result.order, payment_redirect_url=result.payment_redirect_url)


async def _get_pending_demo_order_or_404(session: AsyncSession, order_id: str) -> Order:
    """A ``demo`` order that's still ``pending``, else 404 — identical for
    settled, foreign and nonexistent orders, so this can't probe order state or
    replay a settled payment.
    """
    parsed_id = parse_uuid_or_404(order_id, detail="Demo payment not found.")
    result = await session.execute(
        select(Order)
        .where(Order.id == parsed_id)
        .options(
            selectinload(Order.event),
            selectinload(Order.tickets).selectinload(Ticket.ticket_type),
        )
    )
    order = result.scalar_one_or_none()
    if order is None or order.payment_method != PaymentMethod.DEMO or order.status != OrderStatus.PENDING:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Demo payment not found.")
    return order


def _demo_payment_items(order: Order) -> list[DemoPaymentItemOut]:
    """One line per ticket type, in first-seen order."""
    counts: dict[uuid.UUID, int] = {}
    order_seen: list[uuid.UUID] = []
    for ticket in order.tickets:
        if ticket.ticket_type_id not in counts:
            order_seen.append(ticket.ticket_type_id)
        counts[ticket.ticket_type_id] = counts.get(ticket.ticket_type_id, 0) + 1
    by_id: dict[uuid.UUID, TicketType] = {t.ticket_type_id: t.ticket_type for t in order.tickets}
    return [
        DemoPaymentItemOut(
            ticket_type_name=by_id[tid].name, quantity=counts[tid], unit_price=by_id[tid].price
        )
        for tid in order_seen
    ]


@router.get("/demo-payment/{order_id}")
async def get_demo_payment(order_id: str, session: AsyncSession = Depends(get_session)) -> DemoPaymentOut:
    """Summary for the demo-payment page."""
    order = await _get_pending_demo_order_or_404(session, order_id)
    return DemoPaymentOut(
        order_id=str(order.id),
        event_name=order.event.name,
        buyer_name=order.buyer_name,
        total=order.total,
        language=order.language,
        items=_demo_payment_items(order),
    )


@router.post(
    "/demo-payment/{order_id}/complete",
    dependencies=[Depends(rate_limit_dependency(checkout_rate_limiter))],
)
async def complete_demo_payment(order_id: str, session: AsyncSession = Depends(get_session)) -> OrderOut:
    """"Simulate success": settle like a real payment — sign tickets, issue the
    invoice, and email after commit. No charge, no external call.
    """
    order = await _get_pending_demo_order_or_404(session, order_id)
    try:
        mark_paid_result = await mark_order_paid(
            session,
            order_id=order.id,
            principal=SYSTEM_PRINCIPAL,
            method_label="demo",
            reason="Simulated demo payment: buyer chose 'Simulate successful payment'.",
        )
    except (InsufficientStockError, TicketTypeNotFoundError) as exc:
        # Not reachable (the order was just confirmed PENDING); handled so it can't 500.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Stock is no longer available.") from exc
    if not mark_paid_result.already_paid:
        await sign_order_tickets(session, order=mark_paid_result.order)
        await issue_invoice_for_order(session, order=mark_paid_result.order, principal=SYSTEM_PRINCIPAL)
    await session.commit()
    if not mark_paid_result.already_paid:
        await send_order_confirmation_email(session, order_id=order.id, principal=SYSTEM_PRINCIPAL)
    return await _order_to_out(session, mark_paid_result.order)


@router.post(
    "/demo-payment/{order_id}/fail",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(rate_limit_dependency(checkout_rate_limiter))],
)
async def fail_demo_payment(order_id: str, session: AsyncSession = Depends(get_session)) -> Response:
    """"Simulate failure": cancel the order and release its stock, like a failed Mollie payment."""
    order = await _get_pending_demo_order_or_404(session, order_id)
    await release_order_stock(
        session,
        order_id=order.id,
        new_status=OrderStatus.CANCELLED,
        principal=SYSTEM_PRINCIPAL,
        reason="Simulated demo payment: buyer chose 'Simulate failed payment'.",
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


_MOLLIE_FAILURE_STATUSES: dict[str, OrderStatus] = {
    "expired": OrderStatus.EXPIRED,
    "failed": OrderStatus.CANCELLED,
    "canceled": OrderStatus.CANCELLED,
}
"""Terminal Mollie failures → order status (``failed``/``canceled`` →
CANCELLED, ``expired`` → EXPIRED). Other statuses (``open``, ``pending``,
``authorized``) are still in progress; Mollie will call again.
"""


@router.post(
    "/mollie-webhook",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(rate_limit_dependency(mollie_webhook_rate_limiter))],
)
async def mollie_webhook(request: Request, session: AsyncSession = Depends(get_session)) -> Response:
    """Mollie's webhook — unauthenticated by necessity.

    There's no signature to verify: the body is only ``id=tr_...``, used purely
    as a lookup key, and the real status is fetched from Mollie with the event's
    own key. Idempotent; an order already out of ``PENDING`` short-circuits
    without calling Mollie. Unknown ids and in-progress statuses get 200 (no
    point retrying); transient failures get 502 so Mollie retries.
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

    if order.status != OrderStatus.PENDING:
        # Already settled by an earlier delivery. Skip the Mollie call, so replays
        # (retries, or someone reusing a known payment id) can't generate unbounded
        # outbound calls — the rate limiter alone wouldn't stop that.
        return Response(status_code=status.HTTP_200_OK)

    event = await session.get(Event, order.event_id, options=[selectinload(Event.config)])
    config = event.config if event is not None else None
    # Use the mode pinned at payment creation, not the live config.
    api_key = resolve_mollie_api_key(config, mode=order.mollie_mode)
    if api_key is None:
        # Key cleared mid-payment: fail loudly (502) rather than drop a confirmation.
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Mollie is not configured for this event.")

    try:
        mollie_status = await fetch_mollie_payment_status(api_key=api_key, payment_id=raw_payment_id)
    except MollieApiError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Could not verify payment status with Mollie."
        ) from None

    just_paid = False
    if mollie_status == "paid":
        try:
            mark_paid_result = await mark_order_paid(
                session,
                order_id=order.id,
                principal=SYSTEM_PRINCIPAL,
                method_label="mollie",
                reason="Confirmed via Mollie webhook reconciliation.",
            )
        except (InsufficientStockError, TicketTypeNotFoundError) as exc:
            # Rare: a lapsed order was paid after its stock was resold. This needs a
            # human (refund), not an oversell or endless retries — acknowledge with 200
            # and leave a clear audit trail.
            await record_audit_entry(
                session,
                SYSTEM_PRINCIPAL,
                action="order.mark_paid_conflict",
                target_type="Order",
                target_id=str(order.id),
                detail={"reason": f"Mollie confirmed payment but stock is no longer available: {exc}"},
            )
            await session.commit()
            return Response(status_code=status.HTTP_200_OK)
        just_paid = not mark_paid_result.already_paid
        if just_paid:
            # Sign tickets in the same transaction as the payment.
            await sign_order_tickets(session, order=mark_paid_result.order)
            # Issue the invoice in this transaction too (unlike email, it must be atomic).
            await issue_invoice_for_order(session, order=mark_paid_result.order, principal=SYSTEM_PRINCIPAL)
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

    if just_paid:
        # After commit, and only for a fresh payment (not a retried delivery).
        await send_order_confirmation_email(session, order_id=order.id, principal=SYSTEM_PRINCIPAL)

    return Response(status_code=status.HTTP_200_OK)

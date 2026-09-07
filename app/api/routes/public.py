"""Public, unauthenticated read routes for the landing-page/show/
ticket-type data `frontend-theming` renders (Milestone 2), plus the public
checkout endpoint that creates a ``pending`` Order.

Every route here is intentionally unauthenticated by design (this is the
public buyer-facing site) — the access control that matters is
draft-vs-published + the unguessable preview token (see
``app.models.event.Event.preview_token``), not a login.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.rate_limit import checkout_rate_limiter, rate_limit_dependency
from app.db.session import get_session
from app.models.enums import PublishStatus
from app.models.event import Event
from app.models.order import Order
from app.models.show import Show
from app.models.ticket import Ticket
from app.schemas.order import CheckoutRequest, OrderOut, TicketOut
from app.schemas.public import PublicEventOut, PublicShowOut, PublicThemeOut, PublicTicketTypeOut
from app.services.checkout import CheckoutError, CheckoutItemInput, perform_checkout
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
    """Create a ``pending`` Order (and its Ticket rows) for a public buyer.

    No payment processing happens here — that's Milestone 3 scope. This
    endpoint only performs the row-locked stock check/reservation (see
    ``app.services.stock.reserve_stock``) and persists the Order, per
    PROJECT_BRIEF.md's exact Milestone 2 scope ("checkout form creating a
    'pending' order"). Rate-limited per client IP (see
    ``app.core.rate_limit.checkout_rate_limiter``) per the brief's "rate
    limiting on checkout... endpoints" requirement — this applies from the
    moment this endpoint exists.

    On any validation/availability/stock failure, the transaction is rolled
    back and a specific 403/404/409/422 is returned (see
    ``app.services.checkout.CheckoutError`` and its subclasses) — never an
    unhandled 500 for an expected rejection reason.
    """
    items = [
        CheckoutItemInput(ticket_type_id=_parse_ticket_type_id(item.ticket_type_id), quantity=item.quantity)
        for item in body.items
    ]
    try:
        order = await perform_checkout(
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
    return await _order_to_out(session, order)

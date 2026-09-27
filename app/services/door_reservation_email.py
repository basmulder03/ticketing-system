"""Door-payment reservation email: an order summary sent right after checkout,
with no tickets attached (they don't exist until the order is paid).

Kept separate from ``app.services.ticket_delivery`` on purpose, so an edit
there can never start attaching real, scannable tickets to an unpaid
reservation.
"""

import uuid
from email.message import EmailMessage

import aiosmtplib
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import Principal
from app.models.email_template import EmailTemplate
from app.models.enums import EmailTemplateType, SmtpEncryptionMode
from app.models.event import Event
from app.models.order import Order
from app.models.show import Show
from app.models.ticket import Ticket
from app.models.ticket_type import TicketType
from app.services.audit import record_audit_entry
from app.services.email_render import render_door_payment_confirmation_email

__all__ = ["send_door_payment_confirmation_email"]

_SMTP_TIMEOUT_SECONDS = 20.0


async def _load_order_context(
    session: AsyncSession, order_id: uuid.UUID
) -> tuple[Order, Event, Show, list[Ticket], dict[str, TicketType]] | None:
    """Deliberately not shared with ``ticket_delivery`` (see module doc)."""
    result = await session.execute(
        select(Order)
        .where(Order.id == order_id)
        .options(
            selectinload(Order.event).selectinload(Event.config),
            selectinload(Order.event).selectinload(Event.theme),
            selectinload(Order.tickets).selectinload(Ticket.ticket_type).selectinload(TicketType.show),
        )
    )
    order = result.scalar_one_or_none()
    if order is None or not order.tickets or order.event is None:
        return None
    show = order.tickets[0].ticket_type.show
    ticket_types_by_id = {str(t.ticket_type_id): t.ticket_type for t in order.tickets}
    return order, order.event, show, list(order.tickets), ticket_types_by_id


async def _load_template(session: AsyncSession, *, event_id: uuid.UUID, language: str) -> EmailTemplate | None:
    result = await session.execute(
        select(EmailTemplate).where(
            EmailTemplate.event_id == event_id,
            EmailTemplate.language == language,
            EmailTemplate.template_type == EmailTemplateType.DOOR_PAYMENT_CONFIRMATION.value,
        )
    )
    return result.scalar_one_or_none()


async def send_door_payment_confirmation_email(
    session: AsyncSession, *, order_id: uuid.UUID, principal: Principal
) -> bool:
    """Send the reservation email using the event's SMTP settings, theme and
    template. Never raises; returns ``False`` on failure, audit-logging
    every failure except "order not found". Never signs tickets or attaches PDFs.
    """
    context = await _load_order_context(session, order_id)
    if context is None:
        return False
    order, event, show, tickets, ticket_types_by_id = context

    config = event.config
    if config is None or not config.smtp_host or not config.smtp_port or not config.sender_email:
        await record_audit_entry(
            session,
            principal,
            action="order.door_confirmation_email.failed",
            target_type="Order",
            target_id=str(order.id),
            detail={"reason": "SMTP is not configured for this event."},
        )
        await session.commit()
        return False

    # Broad on purpose: the order is already committed, so any failure here
    # must be logged, never turned into a 500 for the buyer.
    try:
        template = await _load_template(session, event_id=event.id, language=order.language)
        rendered = render_door_payment_confirmation_email(
            template=template,
            order=order,
            event=event,
            show=show,
            theme=event.theme,
            tickets=tickets,
            ticket_types_by_id=ticket_types_by_id,
        )

        message = EmailMessage()
        message["Subject"] = rendered.subject
        message["From"] = (
            f"{config.sender_name} <{config.sender_email}>" if config.sender_name else config.sender_email
        )
        message["To"] = order.buyer_email
        message.set_content(rendered.text_body)
        message.add_alternative(rendered.html_body, subtype="html")

        use_tls = config.smtp_encryption == SmtpEncryptionMode.SSL
        start_tls = config.smtp_encryption == SmtpEncryptionMode.STARTTLS

        await aiosmtplib.send(
            message,
            hostname=config.smtp_host,
            port=config.smtp_port,
            username=config.smtp_username or None,
            password=config.smtp_password or None,
            use_tls=use_tls,
            start_tls=start_tls,
            timeout=_SMTP_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - intentionally broad, see comment above.
        await record_audit_entry(
            session,
            principal,
            action="order.door_confirmation_email.failed",
            target_type="Order",
            target_id=str(order.id),
            detail={"error": str(exc), "recipient": order.buyer_email},
        )
        await session.commit()
        return False

    await record_audit_entry(
        session,
        principal,
        action="order.door_confirmation_email.sent",
        target_type="Order",
        target_id=str(order.id),
        detail={"recipient": order.buyer_email},
    )
    await session.commit()
    return True

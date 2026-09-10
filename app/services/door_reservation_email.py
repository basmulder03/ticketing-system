"""Sends the door-payment reservation-confirmation email (post-launch fix):
a plain order summary, with no ticket attachment, dispatched once right
after checkout for a ``payment_method="door"`` Order — see
``app.services.email_render.render_door_payment_confirmation_email`` for
the full rationale (a buyer had no record of their order at all until it
was later paid at the door; found via the user's own manual testing).

Deliberately its own module, not folded into ``app.services.ticket_delivery``
(that module's whole reason to exist is "what happens once an Order
genuinely becomes paid" — signing real, scannable QR tickets and attaching
them — which is exactly the thing that must NOT happen yet for an unpaid
door reservation). The two modules share the same SMTP-sending shape and
"never let an email failure break a real checkout/payment" discipline, but
intentionally do not share code beyond that, to keep it impossible for a
future edit to one to accidentally start attaching real tickets here.
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
    """Same shape as ``app.services.ticket_delivery``'s private helper of
    the same name (not imported from there — see this module's docstring
    on why the two stay independent)."""
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
    """Render and send the door-payment reservation-confirmation email for
    the Order with ``order_id``, using its Event's own SMTP settings/Theme
    and ``door_payment_confirmation`` EmailTemplate (falling back to a
    built-in EN/NL default).

    Best-effort, same guarantee as
    ``app.services.ticket_delivery.send_order_confirmation_email``: NEVER
    raises. Returns ``True`` on a successful send, ``False`` otherwise
    (Order not found, SMTP not configured for this event, or the send
    itself failed) — every ``False`` case except "Order not found" writes
    a clear audit log entry (action ``order.door_confirmation_email.failed``)
    so a failure is never silently lost.

    Intentionally does NOT sign tickets, issue an invoice, or attach any
    PDF — see this module's and
    ``app.services.email_render.render_door_payment_confirmation_email``'s
    docstrings for why an unpaid door reservation must never carry a real,
    scannable ticket.
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

    # Broad `except Exception`, matching send_order_confirmation_email's
    # own reasoning: a real Order has already been created and committed
    # before this is called (see the checkout route), so a bug or
    # transient failure anywhere in rendering/sending must be logged and
    # swallowed, never allowed to surface as an unhandled 500 to a buyer
    # whose checkout otherwise succeeded.
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

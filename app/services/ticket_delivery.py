"""What happens when an order first becomes paid.

1. :func:`sign_order_tickets` (plus ``issue_invoice_for_order``) run inside
   the payment-confirmation transaction, so QR tokens and the invoice number
   are atomic with it. Both are idempotent.
2. :func:`send_order_confirmation_email` runs *after* commit and sends one
   email with the tickets and invoice PDFs attached. It never raises: an email
   or rendering failure must never undo or fail a real payment. It re-signs
   and re-issues if needed, so the admin resend action can call it standalone.
"""

import uuid
from datetime import UTC, datetime
from email.message import EmailMessage

import aiosmtplib
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import Principal
from app.core.qr_tokens import sign_ticket_token
from app.models.email_template import EmailTemplate
from app.models.enums import EmailTemplateType, SmtpEncryptionMode
from app.models.event import Event
from app.models.order import Order
from app.models.show import Show
from app.models.ticket import Ticket
from app.models.ticket_type import TicketType
from app.services.audit import record_audit_entry
from app.services.email_render import render_order_confirmation_email
from app.services.invoice_pdf import render_invoice_pdf
from app.services.invoicing import issue_invoice_for_order
from app.services.ticket_pdf import render_tickets_pdf

__all__ = ["send_order_confirmation_email", "sign_order_tickets"]

_SMTP_TIMEOUT_SECONDS = 20.0


async def sign_order_tickets(session: AsyncSession, *, order: Order) -> list[Ticket]:
    """Sign a QR token for every unsigned ticket; return all the order's tickets.
    Never re-signs an existing token. Flushes, doesn't commit.
    """
    result = await session.execute(select(Ticket).where(Ticket.order_id == order.id))
    tickets = list(result.scalars().all())
    for ticket in tickets:
        if ticket.qr_token is None:
            ticket.qr_token = sign_ticket_token(ticket.id)
    await session.flush()
    return tickets


async def _load_order_context(
    session: AsyncSession, order_id: uuid.UUID
) -> tuple[Order, Event, Show, list[Ticket], dict[str, TicketType]] | None:
    """Everything needed to render the email, in one query; ``None`` if missing or ticketless."""
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


async def _load_template(
    session: AsyncSession, *, event_id: uuid.UUID, language: str
) -> EmailTemplate | None:
    result = await session.execute(
        select(EmailTemplate).where(
            EmailTemplate.event_id == event_id,
            EmailTemplate.language == language,
            EmailTemplate.template_type == EmailTemplateType.ORDER_CONFIRMATION_TICKET.value,
        )
    )
    return result.scalar_one_or_none()


async def send_order_confirmation_email(
    session: AsyncSession,
    *,
    order_id: uuid.UUID,
    principal: Principal,
    trigger: str = "initial",
) -> bool:
    """Send (or resend) the confirmation email with ticket and invoice PDFs.

    Uses the event's SMTP, theme and template (or built-in defaults). Returns
    ``False`` on any failure, audit-logging it (except "order not found"), and
    never raises. ``trigger`` is only recorded in the audit entry. Commits its own
    transaction.
    """
    context = await _load_order_context(session, order_id)
    if context is None:
        return False
    order, event, show, tickets, ticket_types_by_id = context

    tickets = await sign_order_tickets(session, order=order)
    # Idempotent; only allocates a number if the order somehow has no invoice yet.
    invoice = await issue_invoice_for_order(session, order=order, principal=principal)

    config = event.config
    if config is None or not config.smtp_host or not config.smtp_port or not config.sender_email:
        await record_audit_entry(
            session,
            principal,
            action="order.confirmation_email.failed",
            target_type="Order",
            target_id=str(order.id),
            detail={"reason": "SMTP is not configured for this event.", "trigger": trigger},
        )
        await session.commit()
        return False

    # Catch everything, not just SMTP errors: the payment is already committed,
    # so a rendering bug must be logged, not turned into a 500. (A weasyprint
    # version mismatch once escaped through the checkout route this way.)
    try:
        template = await _load_template(session, event_id=event.id, language=order.language)
        rendered = render_order_confirmation_email(
            template=template,
            order=order,
            event=event,
            show=show,
            theme=event.theme,
            tickets=tickets,
            ticket_types_by_id=ticket_types_by_id,
        )
        pdf_bytes = render_tickets_pdf(
            order=order,
            tickets=tickets,
            ticket_types_by_id=ticket_types_by_id,
            show=show,
            event=event,
            theme=event.theme,
            locale=order.language,
        )
        invoice_pdf_bytes = render_invoice_pdf(
            invoice=invoice,
            order=order,
            event=event,
            theme=event.theme,
            locale=order.language,
        )

        message = EmailMessage()
        message["Subject"] = rendered.subject
        message["From"] = (
            f"{config.sender_name} <{config.sender_email}>" if config.sender_name else config.sender_email
        )
        message["To"] = order.buyer_email
        message.set_content(rendered.text_body)
        message.add_alternative(rendered.html_body, subtype="html")
        message.add_attachment(
            pdf_bytes, maintype="application", subtype="pdf", filename=f"tickets-{order.id}.pdf"
        )
        # The invoice is a second attachment on this same email. The filename uses
        # the order UUID, not the invoice number, whose admin-entered prefix must
        # not reach an email header.
        message.add_attachment(
            invoice_pdf_bytes, maintype="application", subtype="pdf", filename=f"invoice-{order.id}.pdf"
        )

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
        # Never put SMTP credentials in the audit detail.
        await record_audit_entry(
            session,
            principal,
            action="order.confirmation_email.failed",
            target_type="Order",
            target_id=str(order.id),
            detail={"error": str(exc), "recipient": order.buyer_email, "trigger": trigger},
        )
        await session.commit()
        return False

    order.confirmation_email_sent_at = datetime.now(UTC)
    await record_audit_entry(
        session,
        principal,
        action="order.confirmation_email.sent",
        target_type="Order",
        target_id=str(order.id),
        detail={"recipient": order.buyer_email, "trigger": trigger},
    )
    await session.commit()
    return True

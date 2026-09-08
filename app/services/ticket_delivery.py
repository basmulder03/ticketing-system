"""Orchestrates what happens once an Order genuinely becomes ``paid`` for
the first time (Milestone 4): signing each of its Tickets' QR tokens, and
sending the order-confirmation/ticket email (PDF attached) over the Event's
own SMTP settings.

Both real callers of ``app.services.order_payment.mark_order_paid`` that
can observe a fresh (``already_paid=False``) transition today —
the Mollie webhook (``app.api.routes.public.mollie_webhook``) and the
preview-mode simulated-checkout path
(``app.services.checkout._initiate_mollie_payment``) — route through this
module's two functions:

1. :func:`sign_order_tickets` — called INSIDE the same DB transaction as
   the ``mark_order_paid`` status flip (flush-only, no commit), so QR-token
   assignment is atomic with the payment confirmation itself. Idempotent:
   only signs Tickets that don't already have a ``qr_token``.
2. :func:`send_order_confirmation_email` — called AFTER that transaction
   has committed. Sending email is a separate, best-effort side effect
   that must NEVER be allowed to roll back or fail a genuine payment
   confirmation — see PROJECT_BRIEF.md's Security & Ops section
   ("alerting on failed payments or failed email sends"). This function
   never raises: it deliberately catches ANY exception raised while
   rendering the email/PDF or sending it (not just SMTP-specific errors —
   a rendering bug must be treated the same way), writes a clear audit log
   entry either way, and returns a bool instead. It also re-signs any
   unsigned Tickets itself (defensive — see its docstring), which makes it
   safe to call standalone from the admin resend action
   (``app.api.routes.orders.resend_confirmation_email``) without depending
   on step 1 having already run in the same request.
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
from app.services.ticket_pdf import render_tickets_pdf

__all__ = ["send_order_confirmation_email", "sign_order_tickets"]

_SMTP_TIMEOUT_SECONDS = 20.0


async def sign_order_tickets(session: AsyncSession, *, order: Order) -> list[Ticket]:
    """Sign a ``qr_token`` (see ``app.core.qr_tokens.sign_ticket_token``)
    for every Ticket belonging to ``order`` that doesn't already have one,
    and return the full list of the order's Tickets (signed and
    already-signed alike).

    Idempotent by construction (only touches rows where ``qr_token IS
    NULL``), so calling this more than once for the same Order (e.g. once
    from the payment-confirmation path and again from a later resend) never
    re-signs or changes an already-issued token. Flushes but does NOT
    commit — the caller controls the transaction boundary, per this
    module's docstring.
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
    """Fetch everything needed to render/send the confirmation email for
    one Order in a single round trip. Returns ``None`` if the Order doesn't
    exist or (defensively) has no Tickets."""
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
    """Render and send (or re-send) the order-confirmation/ticket email for
    the Order with ``order_id``, using its Event's own SMTP settings,
    Theme, and ``order_confirmation_ticket`` EmailTemplate (falling back to
    a built-in EN/NL default — see ``app.services.email_render`` — if the
    event hasn't customized one).

    Returns ``True`` if the email was sent successfully, ``False``
    otherwise (Order not found/has no tickets, SMTP not configured for
    this event, or the send itself failed) — in every ``False`` case except
    "Order not found", a clear audit log entry has already been written
    (action ``order.confirmation_email.failed``) so a failure is never
    silently lost, per PROJECT_BRIEF.md's "alerting on failed... email
    sends". NEVER raises for an SMTP-level failure — see this module's
    docstring for why that guarantee matters to every caller.

    ``trigger`` is recorded on the audit entry only (``"initial"`` for the
    automatic payment-confirmation path, ``"manual_resend"`` for the admin
    resend action — see ``app.api.routes.orders``) — it has no effect on
    behavior. "Time until the show" is always computed fresh at call time
    (see ``app.services.email_render.compute_days_until_show``), so a
    resend automatically reflects the date getting closer, with no special
    casing needed here.

    Commits its own transaction (any pending ``qr_token`` signing, plus
    ``Order.confirmation_email_sent_at`` on success and the outcome audit
    entry either way) — independent of whatever transaction boundary the
    caller used for the payment-status change itself, per this module's
    docstring.
    """
    context = await _load_order_context(session, order_id)
    if context is None:
        return False
    order, event, show, tickets, ticket_types_by_id = context

    tickets = await sign_order_tickets(session, order=order)

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

    # Everything from here on (rendering the email/PDF, then sending it) is
    # wrapped in one broad `except Exception` — not just the SMTP-specific
    # exception types below. A real payment has already been confirmed and
    # committed by the caller before this function is even invoked (see
    # this module's docstring); a bug or transient failure anywhere in
    # rendering (weasyprint, template substitution, an unexpected data
    # shape) must be treated exactly the same as an SMTP failure — logged
    # clearly and swallowed, never allowed to surface as an unhandled 500
    # to a buyer whose checkout/payment otherwise succeeded. This was
    # verified against a real bug caught during manual end-to-end testing
    # (a weasyprint/pydyf version mismatch raised deep inside
    # ``render_tickets_pdf`` and, before this broad catch was added,
    # propagated all the way out through the checkout route).
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
        # Never include SMTP credentials in the audit detail — `str(exc)`
        # describes the rendering/connection/protocol failure, not the
        # password, but the password itself is never referenced here
        # regardless.
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

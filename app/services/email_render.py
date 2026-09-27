"""Renders the order-confirmation and door-reservation emails.

Resolves the event's EmailTemplate (or the built-in EN/NL default from
``app/i18n``), substitutes placeholders *only* through
``email_placeholders.render_placeholders`` (never Jinja — see that module),
and wraps the result in a table-based, inline-CSS HTML shell plus a
plain-text alternative. Uses only the theme's fixed colors, never custom
CSS.
"""

import html
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal

from app.i18n import SUPPORTED_LOCALES, translate
from app.i18n.formatting import format_currency, format_date, format_time
from app.models.email_template import EmailTemplate
from app.models.enums import EmailTemplateType
from app.models.event import Event
from app.models.order import Order
from app.models.show import Show
from app.models.theme import Theme
from app.models.ticket import Ticket
from app.models.ticket_type import TicketType
from app.services.email_placeholders import render_placeholders
from app.services.theme_images import public_url_for
from app.services.theme_preview import FONT_STACKS
from app.services.ticket_pdf import qr_data_uri

DEFAULT_SUBJECT: dict[str, str] = {
    locale: translate("email.order_confirmation.subject", locale) for locale in SUPPORTED_LOCALES
}
"""Fallback subject per locale when the event has no template (from ``app/i18n``)."""

DEFAULT_BODY: dict[str, str] = {
    locale: translate("email.order_confirmation.body", locale) for locale in SUPPORTED_LOCALES
}
"""Fallback body per locale."""

_SHELL_KEYS: tuple[str, ...] = (
    "heading",
    "days_prefix",
    "days_suffix_plural",
    "days_suffix_singular",
    "days_today",
    "tickets_heading",
    "ticket_type_column",
    "qr_column",
    "qr_alt",
    "attachment_note",
    "logo_alt",
    "footer",
)

_SHELL_STRINGS: dict[str, dict[str, str]] = {
    locale: {key: translate(f"email.order_confirmation.{key}", locale) for key in _SHELL_KEYS}
    for locale in SUPPORTED_LOCALES
}
"""Labels around the body per locale (from ``email.order_confirmation.*``)."""

_DEFAULT_FONT_STACK = "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
_DEFAULT_PRIMARY = "#1a1a1a"
_DEFAULT_SECONDARY = "#ffffff"


def _shell(locale: str) -> dict[str, str]:
    return _SHELL_STRINGS.get(locale, _SHELL_STRINGS["en"])


DOOR_CONFIRMATION_DEFAULT_SUBJECT: dict[str, str] = {
    locale: translate("email.door_confirmation.subject", locale) for locale in SUPPORTED_LOCALES
}
"""Fallback subject per locale for the door-reservation email."""

DOOR_CONFIRMATION_DEFAULT_BODY: dict[str, str] = {
    locale: translate("email.door_confirmation.body", locale) for locale in SUPPORTED_LOCALES
}
"""Fallback body per locale for the door-reservation email."""

_DOOR_CONFIRMATION_SHELL_KEYS: tuple[str, ...] = (
    "heading",
    "order_reference_label",
    "items_heading",
    "ticket_type_column",
    "quantity_column",
    "subtotal_column",
    "total_label",
    "logo_alt",
    "footer",
)

_DOOR_CONFIRMATION_SHELL_STRINGS: dict[str, dict[str, str]] = {
    locale: {key: translate(f"email.door_confirmation.{key}", locale) for key in _DOOR_CONFIRMATION_SHELL_KEYS}
    for locale in SUPPORTED_LOCALES
}
"""Labels around the door-reservation body per locale."""


def _door_confirmation_shell(locale: str) -> dict[str, str]:
    return _DOOR_CONFIRMATION_SHELL_STRINGS.get(locale, _DOOR_CONFIRMATION_SHELL_STRINGS["en"])


def compute_days_until_show(show_date: date, *, now: datetime | None = None) -> int:
    """Days until the show as of now (so resends count down), never negative."""
    reference = (now or datetime.now(UTC)).date()
    return max((show_date - reference).days, 0)


def _days_until_show_line(days: int, locale: str) -> str:
    shell = _shell(locale)
    if days <= 0:
        return shell["days_today"]
    suffix = shell["days_suffix_singular"] if days == 1 else shell["days_suffix_plural"]
    return f"{shell['days_prefix']} {days} {suffix}"


def build_placeholder_values(
    *, order: Order, event: Event, show: Show, days_until_show: int
) -> dict[str, str]:
    """The fixed placeholder values: ``buyer_name`` (buyer input) plus
    server-computed event/show/order data — nothing else.
    """
    return {
        "buyer_name": order.buyer_name,
        "event_name": event.name,
        "show_date": format_date(show.date, order.language),
        "show_time": format_time(show.start_time, order.language),
        "venue_name": show.venue_name,
        "venue_address": show.venue_address,
        "order_total": format_currency(order.total, order.language),
        "days_until_show": str(days_until_show),
    }


@dataclass(frozen=True)
class RenderedEmail:
    """Subject, HTML and plain text for one send."""

    subject: str
    html_body: str
    text_body: str


def _logo_html(theme: Theme | None, event_name: str, locale: str, *, shell: dict[str, str] | None = None) -> str:
    if theme is None or not theme.logo_path:
        # A <p>, not a heading: it only stands in for a missing logo, and a heading
        # here would come before the email's real <h1> and break heading order.
        return f'<p style="margin:0;font-size:18px;font-weight:bold;">{html.escape(event_name)}</p>'
    url = public_url_for(theme.logo_path) or ""
    # Defaults to the order-confirmation labels; the door email passes its own.
    alt = (shell or _shell(locale))["logo_alt"].format(event_name=html.escape(event_name))
    return f'<img src="{html.escape(url)}" alt="{alt}" style="max-height:64px;max-width:220px;display:block;margin:0 auto;" />'


def _ticket_rows_html(tickets: list[Ticket], ticket_types_by_id: dict[str, TicketType], locale: str) -> str:
    shell = _shell(locale)
    rows: list[str] = []
    for ticket in tickets:
        ticket_type = ticket_types_by_id[str(ticket.ticket_type_id)]
        if not ticket.qr_token:
            continue
        qr_uri = qr_data_uri(ticket.qr_token)
        alt = shell["qr_alt"].format(ticket_type=html.escape(ticket_type.name))
        rows.append(
            "<tr>"
            f'<td style="border:1px solid #dddddd;padding:8px;vertical-align:middle;">{html.escape(ticket_type.name)}</td>'
            f'<td style="border:1px solid #dddddd;padding:8px;text-align:center;">'
            f'<img src="{qr_uri}" alt="{alt}" width="120" height="120" style="display:block;margin:0 auto;" />'
            "</td>"
            "</tr>"
        )
    return "\n".join(rows)


def render_order_confirmation_email(
    *,
    template: EmailTemplate | None,
    order: Order,
    event: Event,
    show: Show,
    theme: Theme | None,
    tickets: list[Ticket],
    ticket_types_by_id: dict[str, TicketType],
) -> RenderedEmail:
    """Render the confirmation email using ``template`` or the language default.

    Tickets must already be signed; unsigned ones are skipped rather than
    crashing a send. Days-until-show is computed fresh on every call.
    """
    locale = order.language if order.language in DEFAULT_SUBJECT else "en"
    shell = _shell(locale)
    days_until_show = compute_days_until_show(show.date)
    values = build_placeholder_values(order=order, event=event, show=show, days_until_show=days_until_show)

    raw_subject = template.subject if template is not None else DEFAULT_SUBJECT[locale]
    raw_body = template.body if template is not None else DEFAULT_BODY[locale]
    subject = render_placeholders(raw_subject, values, escape_html=False)
    body_content = render_placeholders(raw_body, values, escape_html=True)

    primary = theme.primary_color if theme is not None else _DEFAULT_PRIMARY
    secondary = theme.secondary_color if theme is not None else _DEFAULT_SECONDARY
    font_stack = FONT_STACKS.get(theme.font_choice, _DEFAULT_FONT_STACK) if theme is not None else _DEFAULT_FONT_STACK

    days_line = _days_until_show_line(days_until_show, locale)
    ticket_rows = _ticket_rows_html(tickets, ticket_types_by_id, locale)
    event_name_escaped = html.escape(event.name)

    # Table layout with inline styles (email-client compatible). Text is always
    # primary on secondary — a pair the theme's AA check vets — and accent is
    # never used for text.
    html_body = f"""<!DOCTYPE html>
<html lang="{html.escape(locale)}">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{html.escape(subject)}</title>
</head>
<body style="margin:0;padding:0;background-color:{secondary};font-family:{font_stack};color:{primary};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:{secondary};">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background-color:{secondary};border:1px solid #dddddd;">
<tr><td style="padding:20px;text-align:center;">
{_logo_html(theme, event.name, locale)}
</td></tr>
<tr><td style="padding:0 24px 8px;">
<h1 style="margin:0 0 16px;font-size:22px;color:{primary};">{html.escape(shell["heading"])}</h1>
{body_content}
<p style="margin:16px 0;padding:12px;border:2px solid {primary};font-size:16px;"><strong>{html.escape(days_line)}</strong></p>
<h2 style="margin:24px 0 8px;font-size:17px;color:{primary};">{html.escape(shell["tickets_heading"])}</h2>
<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
<tr>
<th scope="col" style="border:1px solid #dddddd;padding:8px;text-align:left;">{html.escape(shell["ticket_type_column"])}</th>
<th scope="col" style="border:1px solid #dddddd;padding:8px;text-align:center;">{html.escape(shell["qr_column"])}</th>
</tr>
{ticket_rows}
</table>
<p style="margin-top:16px;font-size:13px;">{html.escape(shell["attachment_note"])}</p>
</td></tr>
<tr><td style="padding:16px;text-align:center;font-size:12px;">{html.escape(shell["footer"].format(event_name=event_name_escaped))}</td></tr>
</table>
</td></tr>
</table>
</body>
</html>
"""

    text_lines = [
        f"{shell['heading']}: {event.name}",
        "",
        _html_to_plain_text(body_content),
        "",
        days_line,
        "",
        shell["tickets_heading"] + ":",
    ]
    for ticket in tickets:
        ticket_type = ticket_types_by_id.get(str(ticket.ticket_type_id))
        if ticket_type is not None:
            text_lines.append(f"- {ticket_type.name}")
    text_lines += ["", shell["attachment_note"]]
    text_body = "\n".join(text_lines)

    return RenderedEmail(subject=subject, html_body=html_body, text_body=text_body)


def _group_tickets_by_type(
    tickets: list[Ticket], ticket_types_by_id: dict[str, TicketType]
) -> list[tuple[TicketType, int]]:
    """``(ticket_type, quantity)`` pairs in first-seen order."""
    counts: dict[str, int] = {}
    order_seen: list[str] = []
    for ticket in tickets:
        tt_id = str(ticket.ticket_type_id)
        if tt_id not in counts:
            order_seen.append(tt_id)
        counts[tt_id] = counts.get(tt_id, 0) + 1
    grouped: list[tuple[TicketType, int]] = []
    for tt_id in order_seen:
        ticket_type = ticket_types_by_id.get(tt_id)
        if ticket_type is not None:
            grouped.append((ticket_type, counts[tt_id]))
    return grouped


def _order_item_rows_html(grouped: list[tuple[TicketType, int]], locale: str) -> str:
    rows: list[str] = []
    for ticket_type, quantity in grouped:
        subtotal = format_currency(ticket_type.price * quantity, locale)
        rows.append(
            "<tr>"
            f'<td style="border:1px solid #dddddd;padding:8px;">{html.escape(ticket_type.name)}</td>'
            f'<td style="border:1px solid #dddddd;padding:8px;text-align:center;">{quantity}</td>'
            f'<td style="border:1px solid #dddddd;padding:8px;text-align:right;">{html.escape(subtotal)}</td>'
            "</tr>"
        )
    return "\n".join(rows)


def render_door_payment_confirmation_email(
    *,
    template: EmailTemplate | None,
    order: Order,
    event: Event,
    show: Show,
    theme: Theme | None,
    tickets: list[Ticket],
    ticket_types_by_id: dict[str, TicketType],
) -> RenderedEmail:
    """Render the door-reservation email: an order summary and amount due, sent
    before any payment.

    Separate from :func:`render_order_confirmation_email` on purpose: it must
    never include real scannable tickets, or a buyer could forward one without
    paying.
    """
    locale = order.language if order.language in DOOR_CONFIRMATION_DEFAULT_SUBJECT else "en"
    shell = _door_confirmation_shell(locale)
    values = build_placeholder_values(
        order=order, event=event, show=show, days_until_show=compute_days_until_show(show.date)
    )

    raw_subject = template.subject if template is not None else DOOR_CONFIRMATION_DEFAULT_SUBJECT[locale]
    raw_body = template.body if template is not None else DOOR_CONFIRMATION_DEFAULT_BODY[locale]
    subject = render_placeholders(raw_subject, values, escape_html=False)
    body_content = render_placeholders(raw_body, values, escape_html=True)

    primary = theme.primary_color if theme is not None else _DEFAULT_PRIMARY
    secondary = theme.secondary_color if theme is not None else _DEFAULT_SECONDARY
    font_stack = FONT_STACKS.get(theme.font_choice, _DEFAULT_FONT_STACK) if theme is not None else _DEFAULT_FONT_STACK

    grouped = _group_tickets_by_type(tickets, ticket_types_by_id)
    item_rows = _order_item_rows_html(grouped, locale)
    event_name_escaped = html.escape(event.name)
    total = format_currency(order.total, locale)

    # Same shell approach as render_order_confirmation_email.
    html_body = f"""<!DOCTYPE html>
<html lang="{html.escape(locale)}">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{html.escape(subject)}</title>
</head>
<body style="margin:0;padding:0;background-color:{secondary};font-family:{font_stack};color:{primary};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:{secondary};">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background-color:{secondary};border:1px solid #dddddd;">
<tr><td style="padding:20px;text-align:center;">
{_logo_html(theme, event.name, locale, shell=shell)}
</td></tr>
<tr><td style="padding:0 24px 8px;">
<h1 style="margin:0 0 16px;font-size:22px;color:{primary};">{html.escape(shell["heading"])}</h1>
{body_content}
<p style="margin:16px 0;font-size:13px;">{html.escape(shell["order_reference_label"])}: {html.escape(str(order.id))}</p>
<h2 style="margin:24px 0 8px;font-size:17px;color:{primary};">{html.escape(shell["items_heading"])}</h2>
<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
<tr>
<th scope="col" style="border:1px solid #dddddd;padding:8px;text-align:left;">{html.escape(shell["ticket_type_column"])}</th>
<th scope="col" style="border:1px solid #dddddd;padding:8px;text-align:center;">{html.escape(shell["quantity_column"])}</th>
<th scope="col" style="border:1px solid #dddddd;padding:8px;text-align:right;">{html.escape(shell["subtotal_column"])}</th>
</tr>
{item_rows}
</table>
<p style="margin:16px 0;padding:12px;border:2px solid {primary};font-size:16px;"><strong>{html.escape(shell["total_label"])}: {html.escape(total)}</strong></p>
</td></tr>
<tr><td style="padding:16px;text-align:center;font-size:12px;">{html.escape(shell["footer"].format(event_name=event_name_escaped))}</td></tr>
</table>
</td></tr>
</table>
</body>
</html>
"""

    text_lines = [
        f"{shell['heading']}: {event.name}",
        "",
        _html_to_plain_text(body_content),
        "",
        f"{shell['order_reference_label']}: {order.id}",
        "",
        shell["items_heading"] + ":",
    ]
    for ticket_type, quantity in grouped:
        text_lines.append(f"- {ticket_type.name} x{quantity}")
    text_lines += ["", f"{shell['total_label']}: {total}"]
    text_body = "\n".join(text_lines)

    return RenderedEmail(subject=subject, html_body=html_body, text_body=text_body)


_SAMPLE_SHOW_DATE = date(2026, 12, 18)


def render_email_template_preview(
    *,
    event: Event,
    theme: Theme | None,
    language: str,
    subject: str,
    body: str,
    template_type: str = EmailTemplateType.ORDER_CONFIRMATION_TICKET.value,
) -> RenderedEmail:
    """Render draft subject/body with sample data and the event's real theme,
    through the exact render path real sends use (chosen by ``template_type``),
    so previews can't drift.
    """
    sample_order = Order(
        event_id=event.id,
        buyer_name="Jamie Sample",
        buyer_email="jamie@example.com",
        buyer_address="1 Example Street",
        total=Decimal("42.50"),
        language=language,
    )
    sample_show = Show(
        event_id=event.id,
        date=_SAMPLE_SHOW_DATE,
        doors_time=time(19, 30),
        start_time=time(20, 0),
        venue_name="Sample Venue",
        venue_address="1 Example Street, Sample Town",
        capacity=100,
    )
    sample_ticket_type = TicketType(show_id=sample_show.id, name="Adult", price=Decimal("42.50"), quantity_available=100)
    sample_ticket = Ticket(order_id=sample_order.id, ticket_type_id=sample_ticket_type.id)
    sample_ticket.qr_token = "PREVIEW-SAMPLE-TOKEN"
    sample_ticket.ticket_type = sample_ticket_type

    template = EmailTemplate(event_id=event.id, language=language, template_type="preview", subject=subject, body=body)
    render = (
        render_door_payment_confirmation_email
        if template_type == EmailTemplateType.DOOR_PAYMENT_CONFIRMATION.value
        else render_order_confirmation_email
    )

    return render(
        template=template,
        order=sample_order,
        event=event,
        show=sample_show,
        theme=theme,
        tickets=[sample_ticket],
        ticket_types_by_id={str(sample_ticket_type.id): sample_ticket_type},
    )


def _html_to_plain_text(fragment: str) -> str:
    """Plain-text version of the admin-authored body only: unescape entities and
    strip tags. Buyer values in it were already escaped upstream.
    """
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = html.unescape(text)
    return " ".join(text.split())

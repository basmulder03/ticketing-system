"""Ticket PDFs via ``weasyprint``, with an embedded QR code: one page per
ticket, A5, print-friendly.

Uses only the theme's fixed fields, never custom CSS. Every interpolated
string (buyer name, event/show/venue names) goes through ``html.escape`` —
without it a name like ``</td><script>`` could break the layout or reach the
renderer as markup.
"""

import base64
import html
from io import BytesIO
from pathlib import Path

import qrcode
from weasyprint import HTML

from app.core.config import get_settings
from app.i18n import translate
from app.i18n.formatting import format_date, format_time
from app.models.event import Event
from app.models.order import Order
from app.models.show import Show
from app.models.theme import Theme
from app.models.ticket import Ticket
from app.models.ticket_type import TicketType
from app.services.theme_preview import FONT_STACKS

__all__ = ["logo_data_uri", "qr_data_uri", "render_tickets_pdf", "render_tickets_pdf_batch"]

_DEFAULT_FONT_STACK = "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
_DEFAULT_PRIMARY = "#1a1a1a"
_DEFAULT_SECONDARY = "#ffffff"
_DEFAULT_ACCENT = "#c9a227"

_QR_BOX_SIZE = 10
"""Pixels per QR module: several hundred px square, crisp at the ~45mm printed size."""


def _esc(value: object) -> str:
    return html.escape(str(value))


def qr_data_uri(payload: str) -> str:
    """The token as a QR PNG data URI — no follow-up fetch, so it displays in
    emails that block remote images and can't act as a tracking pixel.
    """
    image = qrcode.make(payload, box_size=_QR_BOX_SIZE, border=4)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def logo_data_uri(theme: Theme | None) -> str | None:
    """The theme logo as a data URI, or ``None`` (no theme/logo, or file missing).
    Never raises. Also used by ``app.services.invoice_pdf``.
    """
    if theme is None or not theme.logo_path:
        return None
    settings = get_settings()
    path = Path(settings.uploads_dir) / theme.logo_path
    if not path.is_file():
        return None
    suffix = path.suffix.lstrip(".").lower()
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}.get(
        suffix, "image/png"
    )
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _ticket_page_html(
    *,
    ticket: Ticket,
    ticket_type: TicketType,
    order: Order,
    show: Show,
    event: Event,
    logo_uri: str | None,
    primary: str,
    accent: str,
    locale: str,
) -> str:
    if not ticket.qr_token:
        raise ValueError(f"Ticket {ticket.id} has no signed qr_token; call sign_order_tickets first.")

    logo_html = (
        f'<img src="{logo_uri}" alt="{_esc(event.name)} logo" style="max-height:22mm;max-width:60mm;" />'
        if logo_uri
        else f"<h1 style=\"margin:0;font-size:16pt;\">{_esc(event.name)}</h1>"
    )
    qr_uri = qr_data_uri(ticket.qr_token)
    qr_alt = f"QR code for ticket {_esc(str(ticket.id))} ({_esc(ticket_type.name)})"

    return f"""
<section class="ticket-page" style="border:2px solid {accent};padding:10mm;page-break-after:always;">
  <header style="text-align:center;margin-bottom:6mm;">{logo_html}</header>
  <h2 style="font-size:14pt;margin:0 0 4mm;color:{primary};">{_esc(event.name)}</h2>
  <dl style="margin:0 0 6mm;font-size:11pt;color:{primary};">
    <dt style="font-weight:bold;">{_esc(translate("pdf.ticket.show_label", locale))}</dt>
    <dd style="margin:0 0 3mm;">{_esc(format_date(show.date, locale))}, {_esc(format_time(show.start_time, locale))}
      (doors {_esc(format_time(show.doors_time, locale))})</dd>
    <dt style="font-weight:bold;">{_esc(translate("pdf.ticket.venue_label", locale))}</dt>
    <dd style="margin:0 0 3mm;">{_esc(show.venue_name)} — {_esc(show.venue_address)}</dd>
    <dt style="font-weight:bold;">{_esc(translate("pdf.ticket.ticket_type_label", locale))}</dt>
    <dd style="margin:0 0 3mm;">{_esc(ticket_type.name)}</dd>
    <dt style="font-weight:bold;">{_esc(translate("pdf.ticket.ticket_holder_label", locale))}</dt>
    <dd style="margin:0;">{_esc(order.buyer_name)}</dd>
  </dl>
  <div style="text-align:center;">
    <img src="{qr_uri}" alt="{qr_alt}" style="width:45mm;height:45mm;" />
    <p style="font-size:8pt;color:{primary};word-break:break-all;">{_esc(str(ticket.id))}</p>
  </div>
</section>
"""


def _resolve_theme_fields(theme: Theme | None) -> tuple[str, str, str, str, str | None]:
    """``(primary, secondary, accent, font_stack, logo_uri)`` from the fixed
    fields, with defaults when there's no theme.
    """
    primary = theme.primary_color if theme is not None else _DEFAULT_PRIMARY
    secondary = theme.secondary_color if theme is not None else _DEFAULT_SECONDARY
    accent = theme.accent_color if theme is not None else _DEFAULT_ACCENT
    font_stack = FONT_STACKS.get(theme.font_choice, _DEFAULT_FONT_STACK) if theme is not None else _DEFAULT_FONT_STACK
    logo_uri = logo_data_uri(theme)
    return primary, secondary, accent, font_stack, logo_uri


def _order_pages_html(
    *,
    order: Order,
    tickets: list[Ticket],
    ticket_types_by_id: dict[str, TicketType],
    show: Show,
    event: Event,
    logo_uri: str | None,
    primary: str,
    accent: str,
    locale: str,
) -> str:
    """One order's ticket pages, localized to ``locale``."""
    return "\n".join(
        _ticket_page_html(
            ticket=ticket,
            ticket_type=ticket_types_by_id[str(ticket.ticket_type_id)],
            order=order,
            show=show,
            event=event,
            logo_uri=logo_uri,
            primary=primary,
            accent=accent,
            locale=locale,
        )
        for ticket in tickets
    )


def _wrap_document(
    *, pages_html: str, event: Event, primary: str, secondary: str, font_stack: str, locale: str
) -> bytes:
    """Wrap page fragments in one A5 document and render it. Page breaks between
    sections are all it takes to combine many orders into one PDF.
    """
    document_html = f"""<!DOCTYPE html>
<html lang="{_esc(locale)}">
<head>
<meta charset="utf-8" />
<title>{_esc(event.name)} — tickets</title>
<style>
  @page {{ size: A5; margin: 12mm; }}
  body {{ font-family: {font_stack}; color: {primary}; background: {secondary}; margin: 0; }}
  .ticket-page:last-of-type {{ page-break-after: auto; }}
</style>
</head>
<body>
{pages_html}
</body>
</html>
"""
    pdf_bytes: bytes = HTML(string=document_html).write_pdf()
    return pdf_bytes


def render_tickets_pdf(
    *,
    order: Order,
    tickets: list[Ticket],
    ticket_types_by_id: dict[str, TicketType],
    show: Show,
    event: Event,
    theme: Theme | None,
    locale: str,
) -> bytes:
    """One PDF, one page per ticket. Raises ``ValueError`` for an unsigned ticket —
    run ``sign_order_tickets`` first; never print a ticket without a valid code.
    """
    primary, secondary, accent, font_stack, logo_uri = _resolve_theme_fields(theme)
    pages = _order_pages_html(
        order=order,
        tickets=tickets,
        ticket_types_by_id=ticket_types_by_id,
        show=show,
        event=event,
        logo_uri=logo_uri,
        primary=primary,
        accent=accent,
        locale=locale,
    )
    return _wrap_document(
        pages_html=pages, event=event, primary=primary, secondary=secondary, font_stack=font_stack, locale=locale
    )


def render_tickets_pdf_batch(
    *,
    orders_with_tickets: list[tuple[Order, list[Ticket], dict[str, TicketType]]],
    show: Show,
    event: Event,
    theme: Theme | None,
) -> bytes:
    """One combined PDF of every signed ticket across many orders for a show
    (batch printing before an event, via
    ``app.api.routes.orders.download_show_tickets_batch_pdf``).

    One document so staff print once; no PDF-merge dependency needed. Each
    order's pages use that order's own language. Raises ``ValueError`` for an
    unsigned ticket — callers pass only paid orders, so that means a bug. An
    empty input gives a near-empty PDF.
    """
    primary, secondary, accent, font_stack, logo_uri = _resolve_theme_fields(theme)
    pages = "\n".join(
        _order_pages_html(
            order=order,
            tickets=tickets,
            ticket_types_by_id=ticket_types_by_id,
            show=show,
            event=event,
            logo_uri=logo_uri,
            primary=primary,
            accent=accent,
            locale=order.language,
        )
        for order, tickets, ticket_types_by_id in orders_with_tickets
    )
    # <html lang> can hold only one language; each page is already localized,
    # so use the first order's language (or "en").
    doc_locale = orders_with_tickets[0][0].language if orders_with_tickets else "en"
    return _wrap_document(
        pages_html=pages, event=event, primary=primary, secondary=secondary, font_stack=font_stack, locale=doc_locale
    )

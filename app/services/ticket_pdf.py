"""Themed PDF ticket generation (Milestone 4) — ``weasyprint`` (HTML → PDF)
plus ``qrcode`` for the embedded QR code, per PROJECT_BRIEF.md's Ticket
Generation & Delivery section.

Renders every ``Ticket`` belonging to a paid ``Order`` into ONE multi-page
PDF (one ticket per page) — attached once to the confirmation email rather
than as N separate attachments. Sized/margined for reliable print output
(a real page size with sane margins, a QR code large enough to scan
reliably off paper) per the brief's "print-friendly view" requirement;
batch-print across many orders and a standalone backoffice re-print/
download route are out of this milestone's scope (flagged for a later
milestone/`frontend-theming`).

Always uses the Event's Theme FIXED fields (colors/logo/font) — NEVER
``Theme.custom_css`` — per the brief's Event & Theming section: "ticket/
invoice PDFs... always use fixed theme fields for guaranteed compliance".
This module simply never reads ``Theme.custom_css`` at all, the same
enforcement approach ``app.services.email_render`` uses.

Security note: every dynamic string interpolated into the HTML this module
hands to ``weasyprint`` — buyer name (buyer-controlled), event/show/venue/
ticket-type names (agent- or admin-controlled, but still untrusted at this
layer) — is passed through ``html.escape`` before insertion (see ``_esc``).
Without this, a buyer name like ``</td><script>`` could break the ticket's
table layout or, in principle, get evaluated by weasyprint's HTML/CSS
renderer — the same "buyer-submitted text later rendering into a document"
risk class flagged in this project's security history, just for a PDF
instead of an HTML page.
"""

import base64
import html
from io import BytesIO
from pathlib import Path

import qrcode
from weasyprint import HTML

from app.core.config import get_settings
from app.i18n.formatting import format_date, format_time
from app.models.event import Event
from app.models.order import Order
from app.models.show import Show
from app.models.theme import Theme
from app.models.ticket import Ticket
from app.models.ticket_type import TicketType
from app.services.theme_preview import FONT_STACKS

__all__ = ["logo_data_uri", "qr_data_uri", "render_tickets_pdf"]

_DEFAULT_FONT_STACK = "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
_DEFAULT_PRIMARY = "#1a1a1a"
_DEFAULT_SECONDARY = "#ffffff"
_DEFAULT_ACCENT = "#c9a227"

_QR_BOX_SIZE = 10
"""Pixels per QR "module" (the qrcode library's unit). At box_size=10 with
a border of 4 modules, even a short payload (our tokens are a few dozen
ASCII characters) renders at several hundred pixels square — comfortably
enough resolution to stay crisp when printed at the ~45mm on-page size set
in :func:`_ticket_page_html`'s CSS, satisfying the brief's "QR code sized
to scan reliably off paper" requirement."""


def _esc(value: object) -> str:
    return html.escape(str(value))


def qr_data_uri(payload: str) -> str:
    """Render ``payload`` (an HMAC-signed ticket token — see
    ``app.core.qr_tokens.sign_ticket_token``) as a QR code PNG, returned as
    a self-contained ``data:image/png;base64,...`` URI.

    A data URI (not a file reference or external URL) so the resulting
    PDF/HTML email never depends on a follow-up network fetch to display
    the code — important both for reliability (email clients routinely
    block remote images by default) and for privacy (no external request
    that could act as an open/read tracking pixel).
    """
    image = qrcode.make(payload, box_size=_QR_BOX_SIZE, border=4)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def logo_data_uri(theme: Theme | None) -> str | None:
    """Read the theme's logo file (if any) straight off local disk and
    return it as a base64 data URI, for the same "no follow-up fetch"
    reason as :func:`qr_data_uri`. Returns ``None`` if there is no theme,
    no logo configured, or the file is unexpectedly missing on disk (never
    raises — a missing logo degrades to no-logo-image, not a failed PDF).

    Public (not ``ticket``-specific despite living in this module): reused
    as-is by ``app.services.invoice_pdf`` (Milestone 5) since logo
    resolution has nothing ticket-specific about it — DRY rather than a
    second near-identical implementation for the invoice PDF.
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
    <dt style="font-weight:bold;">Show</dt>
    <dd style="margin:0 0 3mm;">{_esc(format_date(show.date, locale))}, {_esc(format_time(show.start_time, locale))}
      (doors {_esc(format_time(show.doors_time, locale))})</dd>
    <dt style="font-weight:bold;">Venue</dt>
    <dd style="margin:0 0 3mm;">{_esc(show.venue_name)} — {_esc(show.venue_address)}</dd>
    <dt style="font-weight:bold;">Ticket type</dt>
    <dd style="margin:0 0 3mm;">{_esc(ticket_type.name)}</dd>
    <dt style="font-weight:bold;">Ticket holder</dt>
    <dd style="margin:0;">{_esc(order.buyer_name)}</dd>
  </dl>
  <div style="text-align:center;">
    <img src="{qr_uri}" alt="{qr_alt}" style="width:45mm;height:45mm;" />
    <p style="font-size:8pt;color:{primary};word-break:break-all;">{_esc(str(ticket.id))}</p>
  </div>
</section>
"""


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
    """Render one PDF containing one page per Ticket in ``tickets`` (all
    assumed to belong to ``order``/``show``), themed with ``event``'s
    Theme fixed fields.

    Raises ``ValueError`` if any Ticket in ``tickets`` has no signed
    ``qr_token`` yet — callers must run
    ``app.services.ticket_delivery.sign_order_tickets`` first; a ticket
    must never be printed/emailed without a scannable code.
    """
    primary = theme.primary_color if theme is not None else _DEFAULT_PRIMARY
    secondary = theme.secondary_color if theme is not None else _DEFAULT_SECONDARY
    accent = theme.accent_color if theme is not None else _DEFAULT_ACCENT
    font_stack = FONT_STACKS.get(theme.font_choice, _DEFAULT_FONT_STACK) if theme is not None else _DEFAULT_FONT_STACK
    logo_uri = logo_data_uri(theme)

    pages = "\n".join(
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
{pages}
</body>
</html>
"""
    pdf_bytes: bytes = HTML(string=document_html).write_pdf()
    return pdf_bytes

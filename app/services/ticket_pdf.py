"""Themed PDF ticket generation (Milestone 4) — ``weasyprint`` (HTML → PDF)
plus ``qrcode`` for the embedded QR code, per PROJECT_BRIEF.md's Ticket
Generation & Delivery section.

Renders every ``Ticket`` belonging to a paid ``Order`` into ONE multi-page
PDF (one ticket per page) — attached once to the confirmation email rather
than as N separate attachments. Sized/margined for reliable print output
(a real page size with sane margins, a QR code large enough to scan
reliably off paper) per the brief's "print-friendly view" requirement.

Milestone 9 adds :func:`render_tickets_pdf_batch`, which generalizes the
exact same "N page-break sections in one HTML string" approach to MANY
Orders at once, for the backoffice's batch-print action
(``GET /api/v1/shows/{show_id}/tickets-batch.pdf``, see
``app.api.routes.shows``) — see that function's docstring for why one
combined PDF (not N separate downloads, not a PDF-merge dependency) is the
right shape.

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
    """Shared theme-field resolution (fixed fields only, never
    ``custom_css`` — see this module's docstring) used by both
    :func:`render_tickets_pdf` and :func:`render_tickets_pdf_batch`, so the
    two never drift on how an absent Theme degrades to defaults.

    Returns ``(primary, secondary, accent, font_stack, logo_uri)``.
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
    """Render one Order's Tickets as concatenated ticket-page HTML
    fragments (one ``<section class="ticket-page">`` per Ticket, via
    :func:`_ticket_page_html`), all labeled/date-formatted in ``locale``.

    Extracted so :func:`render_tickets_pdf` (one Order) and
    :func:`render_tickets_pdf_batch` (many Orders, each keeping its own
    buyer's ``language``) share the exact same per-ticket rendering rather
    than duplicating this loop.
    """
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
    """Wrap already-rendered ticket-page HTML fragments in one A5,
    print-margined ``weasyprint`` document and return the PDF bytes.

    Shared by :func:`render_tickets_pdf` and :func:`render_tickets_pdf_batch`
    — the batch case simply hands this a longer ``pages_html`` string
    (every Order's pages concatenated), which is all "one combined PDF
    across many Orders" actually requires: ``weasyprint`` renders however
    many ``page-break-after`` sections are present in the one HTML string
    into one PDF, with no per-Order document boundary needed.
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
    """Render one PDF containing one page per Ticket in ``tickets`` (all
    assumed to belong to ``order``/``show``), themed with ``event``'s
    Theme fixed fields.

    Raises ``ValueError`` if any Ticket in ``tickets`` has no signed
    ``qr_token`` yet — callers must run
    ``app.services.ticket_delivery.sign_order_tickets`` first; a ticket
    must never be printed/emailed without a scannable code.
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
    """Render ONE combined PDF containing every signed Ticket across
    MULTIPLE Orders for the same Show — backs the backoffice's batch-print
    action per PROJECT_BRIEF.md's Printing section ("a batch-print action
    for multiple/all orders of a show ... printing a full run ahead of an
    event, or reprinting for someone who lost their ticket").

    Deliberately a single multi-page PDF, not N separate per-order
    downloads: a browser print dialog against dozens of separate files is
    not what "batch print ahead of an event" needs — staff want one
    document they can send to a printer once. This reuses the exact same
    mechanism :func:`render_tickets_pdf` already uses to put N tickets from
    ONE Order on N pages of one PDF (``page-break-after`` CSS across
    concatenated HTML sections in a single ``weasyprint`` document),
    generalized here to concatenate pages across MANY Orders instead of
    just one. No PDF-merge dependency (e.g. ``pypdf``) was added or is
    needed: ``weasyprint`` renders as many page-break sections as the input
    HTML string contains into one PDF regardless of which Order each
    section's data came from, so "combine many orders' tickets" is just
    "build a longer HTML string", not a binary-PDF-merge problem.

    Each Order's Tickets are rendered using THAT Order's own ``language``
    (not one shared locale) — an entry in ``orders_with_tickets`` is
    ``(order, tickets, ticket_types_by_id)`` exactly like the parameters
    :func:`render_tickets_pdf` takes for a single Order, so buyers who
    checked out in different languages still get correctly localized
    labels/date formatting on their own pages within the same combined
    document.

    Raises ``ValueError`` (via ``_ticket_page_html``) if any Ticket among
    ``orders_with_tickets`` has no signed ``qr_token`` — callers (see
    ``app.api.routes.shows.download_batch_tickets_pdf``) are expected to
    filter ``orders_with_tickets`` down to Orders whose Tickets are already
    signed (i.e. ``paid`` Orders) themselves, so an unsigned Ticket reaching
    here means a genuine bug worth surfacing loudly rather than silently
    producing an incomplete printout.

    Returns a valid (near-empty) PDF if ``orders_with_tickets`` is empty —
    never raises just for having nothing to render; the caller is expected
    to show a "nothing to print" message instead of calling this in that
    case.
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
    # The document-level `<html lang>` is a single attribute that can't
    # represent a mix of Orders' languages at once (each page's own content
    # is already correctly localized via `_order_pages_html` above) — best
    # effort, defaults to the first Order's language, or "en" if there is
    # nothing to render.
    doc_locale = orders_with_tickets[0][0].language if orders_with_tickets else "en"
    return _wrap_document(
        pages_html=pages, event=event, primary=primary, secondary=secondary, font_stack=font_stack, locale=doc_locale
    )

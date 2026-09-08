"""Themed PDF invoice generation (Milestone 5) — ``weasyprint`` (HTML →
PDF), structurally mirroring ``app.services.ticket_pdf`` (read that
module's docstring first; this one intentionally reuses its escaping/theme
conventions rather than re-deriving them).

Always uses the Event's Theme FIXED fields (colors/logo/font) — NEVER
``Theme.custom_css`` — per PROJECT_BRIEF.md's Event & Theming section:
"ticket/invoice PDFs... always use fixed theme fields for guaranteed
compliance". Reuses ``app.services.ticket_pdf``'s ``logo_data_uri`` helper
rather than duplicating it (DRY) — logo resolution has nothing invoice-
specific about it.

Security note, same discipline as ``ticket_pdf``'s: every dynamic string
interpolated into the HTML here is passed through :func:`_esc`
(``html.escape``) before insertion — this now covers BOTH buyer-submitted
free text (``Order.buyer_name``/``buyer_address``) AND admin-entered
company/VAT details snapshotted on the ``Invoice`` row
(``company_name``/``company_address``/``company_vat_number``), since both
are untrusted at this rendering layer regardless of who originally typed
them.

Field labels ("Invoice", "Bill to", "Qty", etc.) are plain English string
literals below, NOT run through the ``app.i18n`` translation layer —
matching ``app.services.ticket_pdf``'s existing, already-shipped precedent
of hardcoded English field labels for its own ticket fields ("Show",
"Venue", "Ticket type", "Ticket holder") despite ``Order.language`` already
driving date/time formatting there. This is a known pre-existing gap
(flagged again here, not newly introduced) — see this module's handoff for
`content-i18n` to localize both PDFs' field labels together in one pass.
"""

import html
from decimal import Decimal

from weasyprint import HTML

from app.i18n.formatting import format_currency, format_date
from app.models.event import Event
from app.models.invoice import Invoice
from app.models.order import Order
from app.models.theme import Theme
from app.services.theme_preview import FONT_STACKS
from app.services.ticket_pdf import logo_data_uri

__all__ = ["render_invoice_pdf"]

_DEFAULT_FONT_STACK = "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
_DEFAULT_PRIMARY = "#1a1a1a"
_DEFAULT_SECONDARY = "#ffffff"
_DEFAULT_ACCENT = "#c9a227"


def _esc(value: object) -> str:
    return html.escape(str(value))


def _line_items_rows_html(invoice: Invoice, locale: str, primary: str) -> str:
    rows: list[str] = []
    for item in invoice.line_items:
        name = _esc(item["name"])
        quantity = int(str(item["quantity"]))
        unit_price = format_currency(Decimal(str(item["unit_price"])), locale)
        line_total = format_currency(Decimal(str(item["line_total"])), locale)
        rows.append(
            "<tr>"
            f'<td style="border:1px solid #dddddd;padding:6px;">{name}</td>'
            f'<td style="border:1px solid #dddddd;padding:6px;text-align:right;">{quantity}</td>'
            f'<td style="border:1px solid #dddddd;padding:6px;text-align:right;">{_esc(unit_price)}</td>'
            f'<td style="border:1px solid #dddddd;padding:6px;text-align:right;">{_esc(line_total)}</td>'
            "</tr>"
        )
    return "\n".join(rows)


def render_invoice_pdf(
    *,
    invoice: Invoice,
    order: Order,
    event: Event,
    theme: Theme | None,
    locale: str,
) -> bytes:
    """Render ``invoice`` (already issued — see
    ``app.services.invoicing.issue_invoice_for_order``) as a one-page A5
    print-friendly PDF, themed with ``event``'s Theme fixed fields and
    localized to ``locale`` (per PROJECT_BRIEF.md: "PDF generation on
    payment confirmation, localized to buyer's language" — normally
    ``order.language``, since an Invoice's buyer/date/currency formatting
    should always match the same language its Order/tickets were issued
    in).

    Every value rendered here — invoice number/date (server-derived,
    already-safe strings), the company/VAT snapshot, and the buyer name/
    address read live off ``order`` — is escaped via :func:`_esc`. Reads
    ``invoice.line_items`` (frozen at issuance) for the line-item table and
    ``order.total`` for the grand total; never re-reads live ``TicketType``
    rows, per ``app.models.invoice.Invoice``'s module docstring on why that
    would be wrong for an already-issued document.
    """
    primary = theme.primary_color if theme is not None else _DEFAULT_PRIMARY
    secondary = theme.secondary_color if theme is not None else _DEFAULT_SECONDARY
    accent = theme.accent_color if theme is not None else _DEFAULT_ACCENT
    font_stack = FONT_STACKS.get(theme.font_choice, _DEFAULT_FONT_STACK) if theme is not None else _DEFAULT_FONT_STACK
    logo_uri = logo_data_uri(theme)

    logo_html = (
        f'<img src="{logo_uri}" alt="{_esc(event.name)} logo" style="max-height:20mm;max-width:55mm;" />'
        if logo_uri
        else f"<h1 style=\"margin:0;font-size:15pt;\">{_esc(event.name)}</h1>"
    )

    company_block = "<br/>".join(
        _esc(part)
        for part in (
            invoice.company_name,
            invoice.company_address,
            f"VAT {invoice.company_vat_number}" if invoice.company_vat_number else None,
        )
        if part
    )
    buyer_block = "<br/>".join(_esc(part) for part in (order.buyer_name, order.buyer_address) if part)

    rows_html = _line_items_rows_html(invoice, locale, primary)
    total_formatted = _esc(format_currency(order.total, locale))
    issued_date = _esc(format_date(invoice.issued_at.date(), locale))

    document_html = f"""<!DOCTYPE html>
<html lang="{_esc(locale)}">
<head>
<meta charset="utf-8" />
<title>Invoice {_esc(invoice.formatted_number)}</title>
<style>
  @page {{ size: A5; margin: 14mm; }}
  body {{ font-family: {font_stack}; color: {primary}; background: {secondary}; margin: 0; font-size: 10.5pt; }}
  table {{ border-collapse: collapse; width: 100%; }}
</style>
</head>
<body>
<header style="text-align:center;margin-bottom:6mm;">{logo_html}</header>
<h2 style="font-size:14pt;margin:0 0 4mm;color:{primary};border-bottom:2px solid {accent};padding-bottom:2mm;">
  Invoice {_esc(invoice.formatted_number)}
</h2>
<p style="margin:0 0 4mm;">Date: {issued_date}</p>
<table style="margin-bottom:6mm;">
<tr>
  <td style="vertical-align:top;width:50%;padding-right:4mm;">
    <p style="font-weight:bold;margin:0 0 2mm;">From</p>
    <p style="margin:0;">{company_block or "&nbsp;"}</p>
  </td>
  <td style="vertical-align:top;width:50%;">
    <p style="font-weight:bold;margin:0 0 2mm;">Bill to</p>
    <p style="margin:0;">{buyer_block or "&nbsp;"}</p>
  </td>
</tr>
</table>
<table style="margin-bottom:4mm;">
<tr>
  <th scope="col" style="border:1px solid #dddddd;padding:6px;text-align:left;">Item</th>
  <th scope="col" style="border:1px solid #dddddd;padding:6px;text-align:right;">Qty</th>
  <th scope="col" style="border:1px solid #dddddd;padding:6px;text-align:right;">Unit price</th>
  <th scope="col" style="border:1px solid #dddddd;padding:6px;text-align:right;">Line total</th>
</tr>
{rows_html}
</table>
<p style="text-align:right;font-size:12pt;font-weight:bold;border-top:2px solid {accent};padding-top:2mm;">
  Total: {total_formatted}
</p>
</body>
</html>
"""
    pdf_bytes: bytes = HTML(string=document_html).write_pdf()
    return pdf_bytes

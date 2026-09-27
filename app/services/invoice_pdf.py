"""Invoice PDFs via ``weasyprint``, mirroring ``app.services.ticket_pdf``.

Uses only the theme's fixed fields, never custom CSS. Every interpolated
string — buyer input *and* admin-entered company/VAT details — goes through
``html.escape``; the HTML is built by hand rather than with Jinja for the
same reason as ticket PDFs. Labels come from ``pdf.invoice.*`` translations.
"""

import html
from decimal import Decimal

from weasyprint import HTML

from app.i18n import translate
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


def _invoice_html(
    *,
    invoice: Invoice,
    order: Order,
    event: Event,
    theme: Theme | None,
    locale: str,
) -> str:
    """The invoice HTML handed to weasyprint (separate so tests can inspect it).

    Line items come from the frozen ``invoice.line_items``, never live ticket
    types; every value is escaped.
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

    invoice_label = _esc(translate("pdf.invoice.title_label", locale))
    date_label = _esc(translate("pdf.invoice.date_label", locale))
    from_label = _esc(translate("pdf.invoice.from_label", locale))
    bill_to_label = _esc(translate("pdf.invoice.bill_to_label", locale))
    item_column = _esc(translate("pdf.invoice.item_column", locale))
    qty_column = _esc(translate("pdf.invoice.qty_column", locale))
    unit_price_column = _esc(translate("pdf.invoice.unit_price_column", locale))
    line_total_column = _esc(translate("pdf.invoice.line_total_column", locale))
    total_label = _esc(translate("pdf.invoice.total_label", locale))

    document_html = f"""<!DOCTYPE html>
<html lang="{_esc(locale)}">
<head>
<meta charset="utf-8" />
<title>{invoice_label} {_esc(invoice.formatted_number)}</title>
<style>
  @page {{ size: A5; margin: 14mm; }}
  body {{ font-family: {font_stack}; color: {primary}; background: {secondary}; margin: 0; font-size: 10.5pt; }}
  table {{ border-collapse: collapse; width: 100%; }}
</style>
</head>
<body>
<header style="text-align:center;margin-bottom:6mm;">{logo_html}</header>
<h2 style="font-size:14pt;margin:0 0 4mm;color:{primary};border-bottom:2px solid {accent};padding-bottom:2mm;">
  {invoice_label} {_esc(invoice.formatted_number)}
</h2>
<p style="margin:0 0 4mm;">{date_label}: {issued_date}</p>
<table style="margin-bottom:6mm;">
<tr>
  <td style="vertical-align:top;width:50%;padding-right:4mm;">
    <p style="font-weight:bold;margin:0 0 2mm;">{from_label}</p>
    <p style="margin:0;">{company_block or "&nbsp;"}</p>
  </td>
  <td style="vertical-align:top;width:50%;">
    <p style="font-weight:bold;margin:0 0 2mm;">{bill_to_label}</p>
    <p style="margin:0;">{buyer_block or "&nbsp;"}</p>
  </td>
</tr>
</table>
<table style="margin-bottom:4mm;">
<tr>
  <th scope="col" style="border:1px solid #dddddd;padding:6px;text-align:left;">{item_column}</th>
  <th scope="col" style="border:1px solid #dddddd;padding:6px;text-align:right;">{qty_column}</th>
  <th scope="col" style="border:1px solid #dddddd;padding:6px;text-align:right;">{unit_price_column}</th>
  <th scope="col" style="border:1px solid #dddddd;padding:6px;text-align:right;">{line_total_column}</th>
</tr>
{rows_html}
</table>
<p style="text-align:right;font-size:12pt;font-weight:bold;border-top:2px solid {accent};padding-top:2mm;">
  {total_label}: {total_formatted}
</p>
</body>
</html>
"""
    return document_html


def render_invoice_pdf(
    *,
    invoice: Invoice,
    order: Order,
    event: Event,
    theme: Theme | None,
    locale: str,
) -> bytes:
    """Render an issued invoice as a one-page A5 PDF in ``locale`` (normally the order's language)."""
    document_html = _invoice_html(invoice=invoice, order=order, event=event, theme=theme, locale=locale)
    pdf_bytes: bytes = HTML(string=document_html).write_pdf()
    return pdf_bytes

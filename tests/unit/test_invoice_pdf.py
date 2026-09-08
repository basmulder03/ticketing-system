"""Unit tests for ``app.services.invoice_pdf`` (Milestone 5): themed PDF
invoice generation. No DB — mirrors ``tests/unit/test_ticket_pdf.py``'s
"construct throwaway, unpersisted model instances" pattern to exercise the
real render path without needing a Postgres session.

Covers:
- a real, valid PDF is produced (``%PDF`` magic bytes).
- adversarial admin-entered company name/VAT number AND buyer-submitted
  address/name AND a line-item (TicketType) name containing HTML never
  appear unescaped in the HTML-before-render step (tested directly against
  the private ``_invoice_html``/``_esc`` helpers, per PROJECT_BRIEF.md's
  Testing section guidance to test HTML generation directly rather than
  parsing rendered PDF bytes for text content — same approach
  ``test_ticket_pdf.py`` already established).
"""

from datetime import UTC, datetime
from decimal import Decimal

from app.models.event import Event
from app.models.invoice import Invoice
from app.models.order import Order
from app.services.invoice_pdf import _esc, _invoice_html, render_invoice_pdf

_LOCALE = "en"
_ISSUED_AT = datetime(2026, 1, 15, tzinfo=UTC)


def _build_event_order_invoice(
    *,
    company_name: str | None = "Test Company",
    company_address: str | None = "1 Company Street",
    company_vat_number: str | None = "NL000000000B01",
    number_prefix: str = "CP-",
    buyer_name: str = "Jamie Buyer",
    buyer_address: str = "1 Test Street",
    line_item_name: str = "Adult",
    unit_price: str = "15.00",
    total: str = "15.00",
) -> tuple[Event, Order, Invoice]:
    event = Event(name="Test Event", slug="test-event")
    order = Order(
        event_id=event.id,
        buyer_name=buyer_name,
        buyer_email="buyer@example.test",
        buyer_address=buyer_address,
        total=Decimal(total),
        language=_LOCALE,
    )
    invoice = Invoice(
        order_id=order.id,
        event_id=event.id,
        number=7,
        number_prefix=number_prefix,
        issued_at=_ISSUED_AT,
        company_name=company_name,
        company_address=company_address,
        company_vat_number=company_vat_number,
        line_items=[
            {"name": line_item_name, "quantity": 1, "unit_price": unit_price, "line_total": unit_price}
        ],
    )
    return event, order, invoice


# --- _esc ---------------------------------------------------------------


def test_esc_escapes_html_special_characters() -> None:
    assert _esc("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"


def test_esc_stringifies_non_string_input() -> None:
    assert _esc(42) == "42"


# --- Adversarial admin/buyer-entered content in the HTML step -----------


def test_adversarial_company_name_is_escaped_in_invoice_html() -> None:
    event, order, invoice = _build_event_order_invoice(company_name='<script>alert("vat")</script>')
    html = _invoice_html(invoice=invoice, order=order, event=event, theme=None, locale=_LOCALE)
    assert "<script>" not in html
    assert "&lt;script&gt;alert(&quot;vat&quot;)&lt;/script&gt;" in html


def test_adversarial_company_vat_number_is_escaped_in_invoice_html() -> None:
    event, order, invoice = _build_event_order_invoice(company_vat_number="</td><img src=x onerror=alert(1)>")
    html = _invoice_html(invoice=invoice, order=order, event=event, theme=None, locale=_LOCALE)
    assert "<img src=x" not in html
    assert "&lt;/td&gt;&lt;img src=x onerror=alert(1)&gt;" in html


def test_adversarial_buyer_name_is_escaped_in_invoice_html() -> None:
    event, order, invoice = _build_event_order_invoice(buyer_name="<b>Evil</b> Buyer")
    html = _invoice_html(invoice=invoice, order=order, event=event, theme=None, locale=_LOCALE)
    assert "<b>Evil</b>" not in html
    assert "&lt;b&gt;Evil&lt;/b&gt; Buyer" in html


def test_adversarial_buyer_address_is_escaped_in_invoice_html() -> None:
    event, order, invoice = _build_event_order_invoice(buyer_address='<img src=x onerror=alert("addr")>')
    html = _invoice_html(invoice=invoice, order=order, event=event, theme=None, locale=_LOCALE)
    assert "<img src=x" not in html
    assert "&lt;img src=x onerror=alert(&quot;addr&quot;)&gt;" in html


def test_adversarial_line_item_name_is_escaped_in_invoice_html() -> None:
    event, order, invoice = _build_event_order_invoice(line_item_name='<script>alert("tt")</script>')
    html = _invoice_html(invoice=invoice, order=order, event=event, theme=None, locale=_LOCALE)
    assert "<script>" not in html
    assert "&lt;script&gt;alert(&quot;tt&quot;)&lt;/script&gt;" in html


def test_adversarial_event_name_is_escaped_in_invoice_html_logo_fallback() -> None:
    event, order, invoice = _build_event_order_invoice()
    event.name = "</h2><script>alert(1)</script>"
    html = _invoice_html(invoice=invoice, order=order, event=event, theme=None, locale=_LOCALE)
    assert "<script>alert(1)</script>" not in html


# --- Frozen (invoice) vs live (order) fields render from the right source --


def test_invoice_html_renders_the_invoices_own_frozen_number_and_company_snapshot() -> None:
    """The number/prefix/company/VAT block always comes from the Invoice
    row itself, never re-derived from anything else — see
    ``app.models.invoice.Invoice``'s module docstring."""
    event, order, invoice = _build_event_order_invoice(
        company_name="Frozen Co", company_vat_number="NL111111111B01", number_prefix="FROZEN-"
    )
    html = _invoice_html(invoice=invoice, order=order, event=event, theme=None, locale=_LOCALE)
    assert "FROZEN-00007" in html
    assert "Frozen Co" in html
    assert "NL111111111B01" in html


def test_invoice_html_renders_buyer_name_address_and_total_straight_off_order() -> None:
    """Buyer name/address/total are NOT snapshotted onto Invoice — the
    render step reads them straight off the ``Order`` object it's given,
    per this module's docstring."""
    event, order, invoice = _build_event_order_invoice(
        buyer_name="Live Buyer Name", buyer_address="Live Address 42", total="123.45"
    )
    html = _invoice_html(invoice=invoice, order=order, event=event, theme=None, locale=_LOCALE)
    assert "Live Buyer Name" in html
    assert "Live Address 42" in html
    assert "123.45" in html or "123,45" in html  # currency formatting may use either separator


# --- render_invoice_pdf: real PDF output ---------------------------------


def test_render_invoice_pdf_produces_valid_pdf_magic_bytes() -> None:
    event, order, invoice = _build_event_order_invoice()
    pdf_bytes = render_invoice_pdf(invoice=invoice, order=order, event=event, theme=None, locale=_LOCALE)
    assert pdf_bytes.startswith(b"%PDF")
    assert pdf_bytes.rstrip().endswith(b"%%EOF")

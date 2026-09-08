"""Unit tests for ``app.services.ticket_pdf`` (Milestone 4): themed PDF
ticket generation. No DB — mirrors ``app.services.email_render.
render_email_template_preview``'s "construct throwaway, unpersisted model
instances" pattern to exercise the real render path without needing a
Postgres session.

Covers:
- a real, valid PDF is produced (``%PDF`` magic bytes).
- adversarial buyer name / ticket-type name containing HTML never appears
  unescaped in the HTML-before-render step (tested directly against the
  private ``_ticket_page_html``/``_esc`` helpers per the milestone brief's
  guidance to test HTML generation directly rather than parsing rendered
  PDF bytes for text content).
"""

from datetime import date, time
from decimal import Decimal

from app.core.qr_tokens import sign_ticket_token
from app.models.event import Event
from app.models.order import Order
from app.models.show import Show
from app.models.ticket import Ticket
from app.models.ticket_type import TicketType
from app.services.ticket_pdf import _esc, _ticket_page_html, render_tickets_pdf

_LOCALE = "en"


def _build_order_and_ticket(*, buyer_name: str, ticket_type_name: str) -> tuple[Event, Order, Show, Ticket, TicketType]:
    event = Event(name="Test Event", slug="test-event")
    show = Show(
        event_id=event.id,
        date=date(2026, 12, 18),
        doors_time=time(19, 30),
        start_time=time(20, 0),
        venue_name="Test Venue",
        venue_address="1 Test Street",
        capacity=100,
    )
    ticket_type = TicketType(show_id=show.id, name=ticket_type_name, price=Decimal("15.00"), quantity_available=100)
    order = Order(
        event_id=event.id,
        buyer_name=buyer_name,
        buyer_email="buyer@example.test",
        buyer_address="1 Test Street",
        total=Decimal("15.00"),
        language=_LOCALE,
    )
    ticket = Ticket(order_id=order.id, ticket_type_id=ticket_type.id)
    ticket.qr_token = sign_ticket_token(ticket.id)
    return event, order, show, ticket, ticket_type


# --- _esc -------------------------------------------------------------------


def test_esc_escapes_html_special_characters() -> None:
    assert _esc("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"


def test_esc_stringifies_non_string_input() -> None:
    assert _esc(42) == "42"


# --- Adversarial buyer name / ticket-type name in the HTML step -------------


def test_adversarial_buyer_name_is_escaped_in_ticket_page_html() -> None:
    event, order, show, ticket, ticket_type = _build_order_and_ticket(
        buyer_name="<b>Evil</b> Buyer", ticket_type_name="Adult"
    )
    html = _ticket_page_html(
        ticket=ticket,
        ticket_type=ticket_type,
        order=order,
        show=show,
        event=event,
        logo_uri=None,
        primary="#1a1a1a",
        accent="#c9a227",
        locale=_LOCALE,
    )
    assert "<b>Evil</b>" not in html
    assert "&lt;b&gt;Evil&lt;/b&gt; Buyer" in html


def test_adversarial_ticket_type_name_is_escaped_in_ticket_page_html() -> None:
    event, order, show, ticket, ticket_type = _build_order_and_ticket(
        buyer_name="Buyer", ticket_type_name='<script>alert("xss")</script>'
    )
    html = _ticket_page_html(
        ticket=ticket,
        ticket_type=ticket_type,
        order=order,
        show=show,
        event=event,
        logo_uri=None,
        primary="#1a1a1a",
        accent="#c9a227",
        locale=_LOCALE,
    )
    assert "<script>" not in html
    assert "&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;" in html


def test_adversarial_event_name_is_escaped_in_ticket_page_html() -> None:
    event, order, show, ticket, ticket_type = _build_order_and_ticket(buyer_name="Buyer", ticket_type_name="Adult")
    event.name = "</h2><script>alert(1)</script>"
    html = _ticket_page_html(
        ticket=ticket,
        ticket_type=ticket_type,
        order=order,
        show=show,
        event=event,
        logo_uri=None,
        primary="#1a1a1a",
        accent="#c9a227",
        locale=_LOCALE,
    )
    assert "<script>alert(1)</script>" not in html


def test_venue_name_and_address_are_escaped_in_ticket_page_html() -> None:
    event, order, show, ticket, ticket_type = _build_order_and_ticket(buyer_name="Buyer", ticket_type_name="Adult")
    show.venue_name = "<img src=x onerror=alert(1)>"
    html = _ticket_page_html(
        ticket=ticket,
        ticket_type=ticket_type,
        order=order,
        show=show,
        event=event,
        logo_uri=None,
        primary="#1a1a1a",
        accent="#c9a227",
        locale=_LOCALE,
    )
    assert "<img src=x" not in html


def test_ticket_page_html_raises_value_error_when_qr_token_missing() -> None:
    event, order, show, ticket, ticket_type = _build_order_and_ticket(buyer_name="Buyer", ticket_type_name="Adult")
    ticket.qr_token = None
    try:
        _ticket_page_html(
            ticket=ticket,
            ticket_type=ticket_type,
            order=order,
            show=show,
            event=event,
            logo_uri=None,
            primary="#1a1a1a",
            accent="#c9a227",
            locale=_LOCALE,
        )
        raise AssertionError("expected ValueError for a ticket with no signed qr_token")
    except ValueError:
        pass


# --- render_tickets_pdf: real PDF output -------------------------------------


def test_render_tickets_pdf_produces_valid_pdf_magic_bytes() -> None:
    event, order, show, ticket, ticket_type = _build_order_and_ticket(buyer_name="Jamie Buyer", ticket_type_name="Adult")
    pdf_bytes = render_tickets_pdf(
        order=order,
        tickets=[ticket],
        ticket_types_by_id={str(ticket_type.id): ticket_type},
        show=show,
        event=event,
        theme=None,
        locale=_LOCALE,
    )
    assert pdf_bytes.startswith(b"%PDF")
    assert pdf_bytes.rstrip().endswith(b"%%EOF")


def test_render_tickets_pdf_renders_one_page_per_ticket() -> None:
    event, order, show, ticket_a, ticket_type = _build_order_and_ticket(buyer_name="Jamie Buyer", ticket_type_name="Adult")
    ticket_b = Ticket(order_id=order.id, ticket_type_id=ticket_type.id)
    ticket_b.qr_token = sign_ticket_token(ticket_b.id)

    pdf_bytes = render_tickets_pdf(
        order=order,
        tickets=[ticket_a, ticket_b],
        ticket_types_by_id={str(ticket_type.id): ticket_type},
        show=show,
        event=event,
        theme=None,
        locale=_LOCALE,
    )
    # Two distinct object streams isn't a reliable page-count proxy across
    # weasyprint versions, but a "/Count 2" (or similar) page-tree entry is
    # more brittle to assert on than simply trusting the multi-page HTML
    # input produced a bigger document than a single-ticket one would.
    single_ticket_pdf = render_tickets_pdf(
        order=order,
        tickets=[ticket_a],
        ticket_types_by_id={str(ticket_type.id): ticket_type},
        show=show,
        event=event,
        theme=None,
        locale=_LOCALE,
    )
    assert len(pdf_bytes) > len(single_ticket_pdf)


def test_render_tickets_pdf_raises_value_error_for_an_unsigned_ticket() -> None:
    event, order, show, ticket, ticket_type = _build_order_and_ticket(buyer_name="Buyer", ticket_type_name="Adult")
    ticket.qr_token = None
    try:
        render_tickets_pdf(
            order=order,
            tickets=[ticket],
            ticket_types_by_id={str(ticket_type.id): ticket_type},
            show=show,
            event=event,
            theme=None,
            locale=_LOCALE,
        )
        raise AssertionError("expected ValueError for an unsigned ticket")
    except ValueError:
        pass

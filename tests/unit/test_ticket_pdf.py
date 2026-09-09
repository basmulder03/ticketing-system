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

import pytest

import app.services.ticket_pdf as ticket_pdf_module
from app.core.qr_tokens import sign_ticket_token
from app.models.event import Event
from app.models.order import Order
from app.models.show import Show
from app.models.ticket import Ticket
from app.models.ticket_type import TicketType
from app.services.ticket_pdf import (
    _esc,
    _ticket_page_html,
    render_tickets_pdf,
    render_tickets_pdf_batch,
)

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


# --- Field label localization ------------------------------------------


def test_ticket_page_html_uses_english_field_labels_for_en_locale() -> None:
    event, order, show, ticket, ticket_type = _build_order_and_ticket(buyer_name="Buyer", ticket_type_name="Adult")
    html = _ticket_page_html(
        ticket=ticket,
        ticket_type=ticket_type,
        order=order,
        show=show,
        event=event,
        logo_uri=None,
        primary="#1a1a1a",
        accent="#c9a227",
        locale="en",
    )
    assert "<dt style=\"font-weight:bold;\">Show</dt>" in html
    assert "<dt style=\"font-weight:bold;\">Venue</dt>" in html
    assert "<dt style=\"font-weight:bold;\">Ticket type</dt>" in html
    assert "<dt style=\"font-weight:bold;\">Ticket holder</dt>" in html


def test_ticket_page_html_uses_dutch_field_labels_for_nl_locale() -> None:
    event, order, show, ticket, ticket_type = _build_order_and_ticket(buyer_name="Buyer", ticket_type_name="Adult")
    html = _ticket_page_html(
        ticket=ticket,
        ticket_type=ticket_type,
        order=order,
        show=show,
        event=event,
        logo_uri=None,
        primary="#1a1a1a",
        accent="#c9a227",
        locale="nl",
    )
    assert "<dt style=\"font-weight:bold;\">Voorstelling</dt>" in html
    assert "<dt style=\"font-weight:bold;\">Locatie</dt>" in html
    assert "<dt style=\"font-weight:bold;\">Ticketsoort</dt>" in html
    assert "<dt style=\"font-weight:bold;\">Tickethouder</dt>" in html
    # English labels must not leak into the Dutch-locale render.
    assert "<dt style=\"font-weight:bold;\">Show</dt>" not in html
    assert "<dt style=\"font-weight:bold;\">Venue</dt>" not in html


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


# --- render_tickets_pdf_batch (Milestone 9) ----------------------------------


def _build_two_orders_on_one_show() -> tuple[Event, Show, list[tuple[Order, list[Ticket], dict[str, TicketType]]]]:
    """One Show/TicketType shared by two distinct Orders (as
    ``render_tickets_pdf_batch`` expects: one Show/Event for the whole
    batch, each Order still carrying its own ``language``)."""
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
    ticket_type = TicketType(show_id=show.id, name="Adult", price=Decimal("15.00"), quantity_available=100)

    order_en = Order(
        event_id=event.id,
        buyer_name="EN Buyer",
        buyer_email="en-buyer@example.test",
        buyer_address="1 Test Street",
        total=Decimal("15.00"),
        language="en",
    )
    ticket_en = Ticket(order_id=order_en.id, ticket_type_id=ticket_type.id)
    ticket_en.qr_token = sign_ticket_token(ticket_en.id)

    order_nl = Order(
        event_id=event.id,
        buyer_name="NL Buyer",
        buyer_email="nl-buyer@example.test",
        buyer_address="1 Test Street",
        total=Decimal("15.00"),
        language="nl",
    )
    ticket_nl = Ticket(order_id=order_nl.id, ticket_type_id=ticket_type.id)
    ticket_nl.qr_token = sign_ticket_token(ticket_nl.id)

    ticket_types_by_id = {str(ticket_type.id): ticket_type}
    orders_with_tickets = [
        (order_en, [ticket_en], ticket_types_by_id),
        (order_nl, [ticket_nl], ticket_types_by_id),
    ]
    return event, show, orders_with_tickets


def test_render_tickets_pdf_batch_produces_valid_pdf_magic_bytes() -> None:
    event, show, orders_with_tickets = _build_two_orders_on_one_show()
    pdf_bytes = render_tickets_pdf_batch(orders_with_tickets=orders_with_tickets, show=show, event=event, theme=None)
    assert pdf_bytes.startswith(b"%PDF")
    assert pdf_bytes.rstrip().endswith(b"%%EOF")


def test_render_tickets_pdf_batch_includes_pages_for_every_order() -> None:
    """Mirrors ``test_render_tickets_pdf_renders_one_page_per_ticket``'s
    "trust that a bigger HTML input produced a bigger document" signal,
    generalized across Orders instead of within one Order's Tickets."""
    event, show, orders_with_tickets = _build_two_orders_on_one_show()

    both_orders_pdf = render_tickets_pdf_batch(
        orders_with_tickets=orders_with_tickets, show=show, event=event, theme=None
    )
    one_order_pdf = render_tickets_pdf_batch(
        orders_with_tickets=orders_with_tickets[:1], show=show, event=event, theme=None
    )

    assert len(both_orders_pdf) > len(one_order_pdf)


def test_render_tickets_pdf_batch_renders_each_order_in_its_own_language(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each Order's Tickets must be labeled in THAT Order's own
    ``language`` — not one shared locale for the whole batch (see
    ``render_tickets_pdf_batch``'s docstring). Captures the HTML handed to
    ``_wrap_document`` (before it's turned into PDF bytes, which aren't
    reliably greppable for text) via a monkeypatched wrapper that still
    delegates to the real implementation."""
    event, show, orders_with_tickets = _build_two_orders_on_one_show()
    captured: dict[str, str] = {}
    real_wrap_document = ticket_pdf_module._wrap_document

    def _capturing_wrap_document(**kwargs: object) -> bytes:
        captured["pages_html"] = kwargs["pages_html"]  # type: ignore[assignment]
        return real_wrap_document(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ticket_pdf_module, "_wrap_document", _capturing_wrap_document)

    render_tickets_pdf_batch(orders_with_tickets=orders_with_tickets, show=show, event=event, theme=None)

    pages_html = captured["pages_html"]
    # English order's page uses the English field labels...
    assert '<dt style="font-weight:bold;">Ticket holder</dt>' in pages_html
    # ...and the Dutch order's page uses the Dutch ones, in the SAME
    # combined document.
    assert '<dt style="font-weight:bold;">Tickethouder</dt>' in pages_html


def test_render_tickets_pdf_batch_raises_value_error_for_an_unsigned_ticket() -> None:
    event, show, orders_with_tickets = _build_two_orders_on_one_show()
    orders_with_tickets[0][1][0].qr_token = None
    try:
        render_tickets_pdf_batch(orders_with_tickets=orders_with_tickets, show=show, event=event, theme=None)
        raise AssertionError("expected ValueError for an unsigned ticket")
    except ValueError:
        pass


def test_render_tickets_pdf_batch_returns_a_valid_pdf_for_an_empty_batch() -> None:
    """Documented behavior: an empty batch is a valid (near-empty) PDF, not
    an error — the caller is expected to show a "nothing to print" message
    instead of calling this with an empty list, per the function's own
    docstring."""
    event, show, _orders_with_tickets = _build_two_orders_on_one_show()
    pdf_bytes = render_tickets_pdf_batch(orders_with_tickets=[], show=show, event=event, theme=None)
    assert pdf_bytes.startswith(b"%PDF")

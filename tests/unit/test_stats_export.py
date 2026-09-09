"""Unit tests for ``app.services.stats.build_order_export_rows``/
``_ticket_type_summary`` (Milestone 8) — per that function's own docstring,
"pure (no DB access) shaping... trivially unit-testable without a
database". Mirrors ``tests/unit/test_invoice_pdf.py``'s "construct
throwaway, unpersisted model instances" pattern rather than going through a
real session.

Covers:
- ``ticket_types`` cell summarizes multiple TicketTypes/quantities on one
  Order, grouped by name, in first-seen order (e.g. ``"Adult x2; Child
  x1"``).
- ``invoice_number``/``mollie_payment_id`` render as ``""`` (never
  ``"None"``) when absent.
- ``invoice_number`` reflects the Order's real ``Invoice.formatted_number``
  when one is attached.
- one ``OrderExportRow`` per ``Order``, in the order given (the function
  itself does no sorting/filtering — that's the caller's job).
- ``status``/``payment_method``/``total`` render as plain strings, not
  enum reprs or floats.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from app.models.enums import OrderStatus, PaymentMethod
from app.models.invoice import Invoice
from app.models.order import Order
from app.models.ticket import Ticket
from app.models.ticket_type import TicketType
from app.services.stats import CSV_COLUMNS, build_order_export_rows

_CREATED_AT = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def _make_ticket_type(*, name: str = "Adult", price: str = "15.00") -> TicketType:
    return TicketType(name=name, price=Decimal(price), quantity_available=100)


def _make_order(
    *,
    status: OrderStatus = OrderStatus.PAID,
    payment_method: PaymentMethod = PaymentMethod.MOLLIE,
    mollie_payment_id: str | None = None,
    total: str = "15.00",
    buyer_name: str = "Jamie Buyer",
    buyer_email: str = "buyer@example.test",
) -> Order:
    order = Order(
        id=uuid.uuid4(),
        event_id=uuid.uuid4(),
        buyer_name=buyer_name,
        buyer_email=buyer_email,
        buyer_address="1 Test Street",
        status=status,
        payment_method=payment_method,
        mollie_payment_id=mollie_payment_id,
        total=Decimal(total),
        language="en",
        created_at=_CREATED_AT,
    )
    order.tickets = []
    order.invoice = None
    return order


def _attach_tickets(order: Order, *ticket_types: TicketType) -> None:
    tickets = []
    for ticket_type in ticket_types:
        ticket = Ticket(order_id=order.id, ticket_type_id=ticket_type.id)
        ticket.ticket_type = ticket_type
        tickets.append(ticket)
    order.tickets = tickets


# --- ticket_types summary cell -------------------------------------------


def test_ticket_types_summary_groups_by_name_in_first_seen_order_with_counts() -> None:
    adult = _make_ticket_type(name="Adult")
    child = _make_ticket_type(name="Child")
    order = _make_order(total="45.00")
    _attach_tickets(order, adult, adult, child)

    rows = build_order_export_rows([order])

    assert rows[0]["ticket_types"] == "Adult x2; Child x1"


def test_ticket_types_summary_for_a_single_ticket_type() -> None:
    adult = _make_ticket_type(name="Adult")
    order = _make_order(total="15.00")
    _attach_tickets(order, adult)

    rows = build_order_export_rows([order])

    assert rows[0]["ticket_types"] == "Adult x1"


def test_ticket_types_summary_for_an_order_with_no_tickets_is_empty_string() -> None:
    order = _make_order(total="0.00")

    rows = build_order_export_rows([order])

    assert rows[0]["ticket_types"] == ""


# --- invoice_number / mollie_payment_id: empty string, not "None" --------


def test_invoice_number_and_mollie_payment_id_are_empty_strings_when_absent() -> None:
    order = _make_order(payment_method=PaymentMethod.DOOR, mollie_payment_id=None)

    rows = build_order_export_rows([order])

    assert rows[0]["invoice_number"] == ""
    assert rows[0]["mollie_payment_id"] == ""
    assert "None" not in rows[0]["invoice_number"]
    assert "None" not in rows[0]["mollie_payment_id"]


def test_mollie_payment_id_renders_when_present() -> None:
    order = _make_order(payment_method=PaymentMethod.MOLLIE, mollie_payment_id="tr_abc123")

    rows = build_order_export_rows([order])

    assert rows[0]["mollie_payment_id"] == "tr_abc123"


def test_invoice_number_renders_the_invoices_real_formatted_number() -> None:
    order = _make_order()
    invoice = Invoice(
        order_id=order.id,
        event_id=order.event_id,
        number=7,
        number_prefix="CP-",
        issued_at=_CREATED_AT,
        line_items=[],
    )
    order.invoice = invoice

    rows = build_order_export_rows([order])

    assert rows[0]["invoice_number"] == "CP-00007"


# --- One row per Order, in the order given --------------------------------


def test_one_row_per_order_in_input_order() -> None:
    order_a = _make_order(status=OrderStatus.PAID, buyer_name="A")
    order_b = _make_order(status=OrderStatus.CANCELLED, buyer_name="B")
    order_c = _make_order(status=OrderStatus.PENDING_DOOR, buyer_name="C")

    rows = build_order_export_rows([order_a, order_b, order_c])

    assert [row["buyer_name"] for row in rows] == ["A", "B", "C"]
    assert [row["status"] for row in rows] == ["paid", "cancelled", "pending_door"]


def test_build_order_export_rows_on_empty_input_returns_empty_list() -> None:
    assert build_order_export_rows([]) == []


# --- Plain string rendering, not enum reprs / floats -----------------------


def test_status_and_payment_method_render_as_plain_enum_values() -> None:
    order = _make_order(status=OrderStatus.PENDING, payment_method=PaymentMethod.DOOR)

    rows = build_order_export_rows([order])

    assert rows[0]["status"] == "pending"
    assert rows[0]["payment_method"] == "door"


def test_total_renders_as_exact_decimal_string_not_float() -> None:
    order = _make_order(total="1234.50")

    rows = build_order_export_rows([order])

    assert rows[0]["total"] == "1234.50"


def test_row_keys_match_csv_columns_exactly() -> None:
    order = _make_order()

    rows = build_order_export_rows([order])

    assert set(rows[0].keys()) == set(CSV_COLUMNS)

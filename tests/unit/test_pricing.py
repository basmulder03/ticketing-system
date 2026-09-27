"""Unit tests for ``app.services.pricing`` (order-total/service-fee math),
pure and DB-free — see that module's docstring for the flag's actual
polarity: ``service_fee_included=True`` (the default) means the price
already includes the fee, so ``False`` is what triggers charging it.
"""

import uuid
from decimal import Decimal

from app.models.ticket import Ticket
from app.models.ticket_type import TicketType
from app.services.pricing import compute_order_pricing, derive_order_subtotal_and_fee


def _ticket_type(*, price: str, service_fee_included: bool) -> TicketType:
    return TicketType(
        id=uuid.uuid4(),
        show_id=uuid.uuid4(),
        name="Adult",
        price=Decimal(price),
        quantity_available=100,
        service_fee_included=service_fee_included,
    )


def _ticket(ticket_type: TicketType) -> Ticket:
    """A transient (never flushed) ``Ticket`` with just ``.ticket_type`` set —
    all ``derive_order_subtotal_and_fee`` reads.
    """
    ticket = Ticket(order_id=uuid.uuid4(), ticket_type_id=ticket_type.id)
    ticket.ticket_type = ticket_type
    return ticket


def test_no_fee_when_every_ticket_type_already_includes_it() -> None:
    tt = _ticket_type(price="20.00", service_fee_included=True)
    pricing = compute_order_pricing({tt.id: tt}, {tt.id: 2}, service_fee_amount=Decimal("1.50"))

    assert pricing.subtotal == Decimal("40.00")
    assert pricing.service_fee_total == Decimal("0.00")
    assert pricing.total == Decimal("40.00")


def test_fee_charged_once_per_ticket_when_not_included() -> None:
    tt = _ticket_type(price="20.00", service_fee_included=False)
    pricing = compute_order_pricing({tt.id: tt}, {tt.id: 3}, service_fee_amount=Decimal("1.50"))

    assert pricing.subtotal == Decimal("60.00")
    assert pricing.service_fee_total == Decimal("4.50")
    assert pricing.total == Decimal("64.50")


def test_fee_amount_of_zero_never_adds_anything_even_when_not_included() -> None:
    tt = _ticket_type(price="20.00", service_fee_included=False)
    pricing = compute_order_pricing({tt.id: tt}, {tt.id: 2}, service_fee_amount=Decimal("0.00"))

    assert pricing.service_fee_total == Decimal("0.00")
    assert pricing.total == pricing.subtotal


def test_mixed_order_only_charges_the_fee_on_the_eligible_line() -> None:
    included = _ticket_type(price="20.00", service_fee_included=True)
    not_included = _ticket_type(price="10.00", service_fee_included=False)
    ticket_types = {included.id: included, not_included.id: not_included}
    quantities = {included.id: 2, not_included.id: 4}

    pricing = compute_order_pricing(ticket_types, quantities, service_fee_amount=Decimal("2.00"))

    assert pricing.subtotal == Decimal("80.00")  # 2*20 + 4*10
    assert pricing.service_fee_total == Decimal("8.00")  # 4 * 2.00
    assert pricing.total == Decimal("88.00")
    assert pricing.subtotal + pricing.service_fee_total == pricing.total


def test_derive_order_subtotal_and_fee_reads_back_a_consistent_split() -> None:
    tt = _ticket_type(price="20.00", service_fee_included=False)
    tickets = [_ticket(tt), _ticket(tt)]

    subtotal, fee_total = derive_order_subtotal_and_fee(Decimal("44.50"), tickets)

    assert subtotal == Decimal("40.00")
    assert fee_total == Decimal("4.50")
    assert subtotal + fee_total == Decimal("44.50")


def test_derive_order_subtotal_and_fee_clamps_at_zero_if_total_is_lower_than_subtotal() -> None:
    """A ticket type's price raised after purchase (no per-order price
    snapshot exists — see ``OrderOut``'s docstring) must never surface as a
    negative fee.
    """
    tt = _ticket_type(price="99.00", service_fee_included=True)
    tickets = [_ticket(tt)]

    subtotal, fee_total = derive_order_subtotal_and_fee(Decimal("20.00"), tickets)

    assert subtotal == Decimal("99.00")
    assert fee_total == Decimal("0.00")

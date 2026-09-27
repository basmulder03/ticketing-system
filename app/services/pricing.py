"""Order pricing: ticket price times quantity, plus a flat per-ticket service
fee added on top for ticket types where the price does NOT already include
one (``TicketType.service_fee_included`` is ``False`` — the flag names what's
already true of the price, not whether a fee gets charged).

Shared by ``app.services.checkout`` and ``app.services.manual_order`` so the
two order-creation paths can never compute a total differently. The fee
itself is charged consistently regardless of channel (online, door or
staff-issued) — it's a property of the ticket type, not of how the order
was placed.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from app.models.ticket_type import TicketType

if TYPE_CHECKING:
    from app.models.ticket import Ticket

__all__ = ["OrderPricing", "compute_order_pricing", "derive_order_subtotal_and_fee"]


@dataclass(frozen=True)
class OrderPricing:
    """A breakdown that always satisfies ``subtotal + service_fee_total == total``."""

    subtotal: Decimal
    service_fee_total: Decimal
    total: Decimal


def compute_order_pricing(
    ticket_types: dict[uuid.UUID, TicketType],
    quantities: dict[uuid.UUID, int],
    *,
    service_fee_amount: Decimal,
) -> OrderPricing:
    """``ticket_types``/``quantities`` share keys (as ``reserve_stock`` returns).

    ``service_fee_amount`` is charged once per ticket (not per line) for every
    ticket whose type has ``service_fee_included=False`` (its price doesn't
    already include the fee).
    """
    subtotal = Decimal("0.00")
    fee_eligible_count = 0
    for ticket_type_id, quantity in quantities.items():
        ticket_type = ticket_types[ticket_type_id]
        subtotal += ticket_type.price * quantity
        if not ticket_type.service_fee_included:
            fee_eligible_count += quantity

    service_fee_total = service_fee_amount * fee_eligible_count
    return OrderPricing(subtotal=subtotal, service_fee_total=service_fee_total, total=subtotal + service_fee_total)


def derive_order_subtotal_and_fee(order_total: Decimal, tickets: "Sequence[Ticket]") -> tuple[Decimal, Decimal]:
    """``(subtotal, service_fee_total)`` for an already-created order, read back
    from its tickets' *current* ticket-type prices.

    Not the same computation as :func:`compute_order_pricing`: there's no
    per-order price snapshot (see ``OrderOut``'s docstring), so this is a
    best-effort breakdown of the frozen ``order_total`` rather than an
    independent recomputation — ``service_fee_total`` is clamped at zero so a
    ticket-type price raised after purchase can't show as a negative fee.
    """
    subtotal = sum((t.ticket_type.price for t in tickets), Decimal("0.00"))
    service_fee_total = max(Decimal("0.00"), order_total - subtotal)
    return subtotal, service_fee_total

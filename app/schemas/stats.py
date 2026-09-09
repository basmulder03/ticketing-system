"""Response schemas for the Milestone 8 Stats & Reporting dashboard.

See ``app.services.stats`` for the aggregation queries these shapes are
built from, and in particular that module's docstring for two judgment
calls baked into these fields: which ``OrderStatus`` values count as
"revenue", and why there is deliberately no net-of-fees figure anywhere
here.
"""

from datetime import date as date_type
from decimal import Decimal

from pydantic import BaseModel

from app.models.enums import PaymentMethod


class TicketTypeStatsOut(BaseModel):
    """Sales figures for one ``TicketType`` under a ``Show``.

    ``sold``/``revenue`` use the same "live" counting rule as
    ``app.services.stock.sold_counts_for_ticket_types`` — every ``Ticket``
    row belonging to a non-``cancelled``/non-``expired`` Order, which
    INCLUDES still-``pending``/``pending_door`` orders that haven't
    actually settled any money yet. This is deliberately a different
    population from ``EventStatsOut.revenue_total``/
    ``revenue_by_payment_method`` below, which count only ``paid`` orders —
    see ``app.services.stats`` module docstring for why these two figures
    are intentionally not the same number (and won't sum to match each
    other while an event has any pending/door-reserved orders in flight).
    """

    id: str
    name: str
    sold: int
    revenue: Decimal
    scanned: int


class ShowStatsOut(BaseModel):
    """Sales and scan-in figures for one ``Show``, plus its per-TicketType
    breakdown. ``sold_total``/``scanned_total`` are simply the sum of the
    same fields across ``ticket_types`` — provided pre-summed so a
    dashboard doesn't need to re-derive them client-side.
    """

    id: str
    date: date_type
    venue_name: str
    ticket_types: list[TicketTypeStatsOut]
    sold_total: int
    scanned_total: int


class PaymentMethodRevenueOut(BaseModel):
    """Settled (``paid``-order) revenue for one ``PaymentMethod``.

    Always one entry per ``PaymentMethod`` value (``mollie`` and ``door``),
    even if an Event has zero paid orders for a given method — so a
    dashboard can render a fixed online/door split without special-casing
    an absent method.
    """

    payment_method: PaymentMethod
    order_count: int
    revenue: Decimal


class EventStatsOut(BaseModel):
    """The Milestone 8 dashboard payload for one Event.

    ``revenue_total``/``revenue_by_payment_method`` are GROSS revenue only
    — the real amount charged to buyers (``Order.total``), summed across
    every ``paid`` Order for this Event. There is deliberately no
    net-of-fees figure anywhere in this schema: see ``app.services.stats``
    module docstring for why (Mollie exposes no per-payment fee data
    reachable via the simple per-event API key this app is built around;
    getting real fee data would need an entirely different, OAuth-based
    Mollie credential model, which is out of scope for this milestone).
    This is a deliberate, known scope reduction, not a placeholder for a
    future fee estimate.

    ``sold_total``/``scanned_total`` are the Event-wide sums of the
    same-named per-Show fields (the "live" sold/scanned population — see
    ``TicketTypeStatsOut`` docstring), provided for convenience alongside
    the per-Show breakdown in ``shows``.
    """

    event_id: str
    shows: list[ShowStatsOut]
    revenue_total: Decimal
    revenue_by_payment_method: list[PaymentMethodRevenueOut]
    sold_total: int
    scanned_total: int

"""Response models for the stats dashboard (aggregation in ``app.services.stats``)."""

from datetime import date as date_type
from decimal import Decimal

from pydantic import BaseModel

from app.models.enums import PaymentMethod


class TicketTypeStatsOut(BaseModel):
    """Sales for one ticket type.

    ``sold``/``revenue`` count every ticket on a non-cancelled/expired order,
    *including* pending and door reservations — unlike the event-level revenue
    figures, which count only paid orders. The two won't match while orders are
    pending, by design.
    """

    id: str
    name: str
    sold: int
    revenue: Decimal
    scanned: int


class ShowStatsOut(BaseModel):
    """One show's sales and scan-ins, with totals pre-summed from ``ticket_types``."""

    id: str
    date: date_type
    venue_name: str
    ticket_types: list[TicketTypeStatsOut]
    sold_total: int
    scanned_total: int


class PaymentMethodRevenueOut(BaseModel):
    """Paid-order revenue for one payment method. There's always one entry per
    ``PaymentMethod``, even at zero, so the dashboard needn't special-case gaps.
    """

    payment_method: PaymentMethod
    order_count: int
    revenue: Decimal


class EventStatsOut(BaseModel):
    """Dashboard payload for one event.

    Revenue is gross (what buyers paid) over paid orders only. There's no
    net-of-fees figure: Mollie exposes fees only via an OAuth API this app's
    per-event API keys can't reach.
    """

    event_id: str
    shows: list[ShowStatsOut]
    revenue_total: Decimal
    revenue_by_payment_method: list[PaymentMethodRevenueOut]
    sold_total: int
    scanned_total: int

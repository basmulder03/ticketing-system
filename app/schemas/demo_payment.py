"""Pydantic response model for the demo payment provider's read endpoint.

Post-launch fix, per the user's NOTES: "create a custom [payment method],
that behaves something like mollie for a test environment/demo purposes
without having to do stuff with external applications." See
``app.services.checkout._initiate_demo_payment`` and
``app.web.routes.demo_payment`` for the full flow this backs.
"""

from decimal import Decimal

from pydantic import BaseModel


class DemoPaymentItemOut(BaseModel):
    """One line item on the demo-payment summary — grouped by ticket type,
    not one row per individual Ticket (a buyer reviewing what they're
    about to "pay" for wants "2x Adult", not two identical rows)."""

    ticket_type_name: str
    quantity: int
    unit_price: Decimal


class DemoPaymentOut(BaseModel):
    """Response of ``GET /api/v1/public/demo-payment/{order_id}`` — just
    enough for the demo-payment page to show the buyer what they're about
    to simulate paying for. Only ever returned for an Order that is
    ``payment_method=demo`` AND still ``pending`` (see
    ``app.api.routes.public._get_pending_demo_order_or_404``) — a
    completed/failed/foreign/nonexistent order 404s identically, the same
    "can't distinguish doesn't-exist from not-eligible" posture this app
    already uses for draft events (``app.services.checkout``'s
    ``EventNotAvailableCheckoutError``)."""

    order_id: str
    event_name: str
    buyer_name: str
    total: Decimal
    language: str
    items: list[DemoPaymentItemOut]

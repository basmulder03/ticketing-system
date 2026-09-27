"""Response models for the demo payment provider's summary page."""

from decimal import Decimal

from pydantic import BaseModel


class DemoPaymentItemOut(BaseModel):
    """One summary line per ticket type ("2x Adult"), not per ticket."""

    ticket_type_name: str
    quantity: int
    unit_price: Decimal


class DemoPaymentOut(BaseModel):
    """What the demo-payment page shows. Only returned for a ``demo`` order that's
    still ``pending``; anything else 404s identically.
    """

    order_id: str
    event_name: str
    buyer_name: str
    total: Decimal
    language: str
    items: list[DemoPaymentItemOut]

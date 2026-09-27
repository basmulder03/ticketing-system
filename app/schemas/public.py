"""Response models for the public read routes: an event with its shows,
ticket types and theme (everything the landing page and SEO/JSON-LD need).
"""

from datetime import date as date_type
from datetime import datetime
from datetime import time as time_type
from decimal import Decimal

from pydantic import BaseModel

from app.models.enums import PaymentMethod, PublishStatus, ThemeFont


class PublicThemeOut(BaseModel):
    """An event's public branding. ``custom_css`` was sanitized on save and is served as stored."""

    primary_color: str
    secondary_color: str
    accent_color: str
    font_choice: ThemeFont
    logo_url: str | None
    background_image_url: str | None
    custom_css: str | None


class PublicTicketTypeOut(BaseModel):
    """A ticket type with live ``remaining`` stock."""

    id: str
    name: str
    price: Decimal
    service_fee_included: bool
    quantity_available: int
    remaining: int


class PublicShowOut(BaseModel):
    """A show in a public/preview response. ``status`` is always ``published`` on
    the public route; it's kept so both routes share one shape.
    """

    id: str
    date: date_type
    doors_time: time_type
    start_time: time_type
    venue_name: str
    venue_address: str
    capacity: int
    status: PublishStatus
    ticket_types: list[PublicTicketTypeOut]


class PublicEventOut(BaseModel):
    """A published event (or, via the preview route, a draft one).
    ``is_preview`` marks draft content so pages render ``noindex`` and a
    preview banner.
    """

    id: str
    name: str
    slug: str
    description: str | None
    status: PublishStatus
    sales_paused: bool
    sales_live_at: datetime | None
    enabled_payment_methods: list[PaymentMethod]
    theme: PublicThemeOut | None
    shows: list[PublicShowOut]
    is_preview: bool


class PublicEventSummaryOut(BaseModel):
    """One homepage directory row — just enough to pick an event; the full
    payload loads on its own page.
    """

    name: str
    slug: str
    description: str | None


class PublicHomepageOut(BaseModel):
    """Homepage data. ``default_event_slug`` is set only when the default event
    is also published, so a draft default has no visible effect yet.
    """

    default_event_slug: str | None
    events: list[PublicEventSummaryOut]

"""Pydantic response models for the public-site read routes (Milestone 2):
published/preview Event detail, nested Shows and TicketTypes, and the
Theme's public-facing fields. See ``app.api.routes.public`` for the routes
these back, and PROJECT_BRIEF.md's SEO & Discoverability section for why
every field a ``schema.org/Event`` JSON-LD block needs (name, startDate,
location, offers/pricing, availability) must round-trip through here —
`frontend-theming`/`content-i18n` render the actual meta tags/JSON-LD from
these fields, this module only guarantees the data is present.
"""

from datetime import date as date_type
from datetime import datetime
from datetime import time as time_type
from decimal import Decimal

from pydantic import BaseModel

from app.models.enums import PaymentMethod, PublishStatus, ThemeFont


class PublicThemeOut(BaseModel):
    """An Event's public branding fields.

    ``custom_css`` is served exactly as stored — it was already sanitized
    once at save time (see ``app.core.css_sanitizer.sanitize_custom_css``,
    Milestone 1.5) and is never re-sanitized here.
    """

    primary_color: str
    secondary_color: str
    accent_color: str
    font_choice: ThemeFont
    logo_url: str | None
    background_image_url: str | None
    custom_css: str | None


class PublicTicketTypeOut(BaseModel):
    """A purchasable ticket type, with live ``remaining`` stock — always
    computed fresh via ``app.services.stock.attach_remaining``, never
    ``quantity_available`` directly."""

    id: str
    name: str
    price: Decimal
    service_fee_included: bool
    quantity_available: int
    remaining: int


class PublicShowOut(BaseModel):
    """A Show under a public/preview Event response.

    ``status`` is included even on the published-only route (where it's
    always ``published``, since unpublished Shows are filtered out before
    this is built) so the preview and published routes share one response
    shape — `frontend-theming` doesn't need two schemas.
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
    """A published (or, via the preview-token route, draft) Event, with
    everything `frontend-theming` needs for the landing page and SEO/
    structured-data: name/description for title & meta description, each
    Show's date/venue for ``startDate``/``location``, and each
    TicketType's price/remaining for ``offers``/``availability``.

    ``is_preview`` tells the caller whether this response came from the
    unguessable preview-token route (draft content — e.g. so
    `frontend-theming` renders a ``noindex`` meta tag and a "draft preview"
    banner) rather than the normal published-slug route.
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

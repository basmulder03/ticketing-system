"""Request/response models backing the scanner-facing "which show am I
scanning for" surface (Milestone 7 frontend) — ``GET /api/v1/scan/shows``
(list) and ``GET /api/v1/scan/shows/{show_id}`` (single), both defined in
``app.api.routes.scan_shows``.

Split out from ``app.schemas.show`` deliberately: ``ShowOut`` (that module)
is the content-management shape returned by the admin/agent-only
``app.api.routes.shows`` CRUD routes, which never include the parent
Event's name (a caller there already has the Event in hand, since every one
of those routes is nested under ``/api/v1/events/{event_id}/...``). This
module's routes are NOT event-nested (a scanner picks a show without
already knowing/holding an event id) and exist specifically to hand a
scanner/admin frontend everything it needs to label a show ("Event name —
date, venue") and to thread ``event_id`` into the scan page for the
unpaid -> mark-as-paid form, in one response, without a second lookup.
"""

from datetime import date as date_type
from datetime import time as time_type

from pydantic import BaseModel

from app.models.enums import PublishStatus


class ScannableShowOut(BaseModel):
    """One Show a scanner/admin principal may pick to scan for."""

    id: str
    event_id: str
    event_name: str
    date: date_type
    doors_time: time_type
    start_time: time_type
    venue_name: str
    status: PublishStatus

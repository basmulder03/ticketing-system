"""Response model for the scanner's "which show am I scanning for" picker.

Unlike ``ShowOut``, these routes aren't nested under an event, so each row
carries the event's name and id for labeling and the mark-as-paid form.
"""

from datetime import date as date_type
from datetime import time as time_type

from pydantic import BaseModel

from app.models.enums import PublishStatus


class ScannableShowOut(BaseModel):
    """One show a scanner may pick."""

    id: str
    event_id: str
    event_name: str
    date: date_type
    doors_time: time_type
    start_time: time_type
    venue_name: str
    status: PublishStatus

"""Pydantic request/response models for ``EmailTemplate`` CRUD and its
draft-values preview endpoint (Milestone 4).
"""

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.i18n import SUPPORTED_LOCALES
from app.models.enums import EmailTemplateType

_SUPPORTED_TEMPLATE_TYPES = {t.value for t in EmailTemplateType}


class EmailTemplateUpsertRequest(BaseModel):
    """Body of ``PUT /api/v1/events/{event_id}/email-templates/{template_type}/{language}``.

    Both fields are required (unlike Theme/EventConfig's partial-update
    ``exclude_unset`` convention) — a template row is only ever meaningful
    with both a subject and a body present at once, so a partial update
    that left one half stale/empty isn't a useful operation to support.
    """

    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=50_000)


class EmailTemplateOut(BaseModel):
    """Response shape for one EmailTemplate row."""

    id: str
    event_id: str
    language: str
    template_type: str
    subject: str
    body: str
    created_at: datetime
    updated_at: datetime


class EmailTemplatePreviewRequest(BaseModel):
    """Body of ``POST /api/v1/events/{event_id}/email-templates/preview``.

    Renders arbitrary, not-yet-saved subject/body/language values against
    representative sample placeholder data — nothing is persisted, mirrors
    ``app.schemas.theme.ThemePreviewRequest``'s pattern for the same
    "let the backoffice show a live preview before saving" need
    (PROJECT_BRIEF.md: "a preview using real theme colors/logo before
    saving"). ``template_type`` is accepted for forward-compatibility (so
    the preview endpoint's shape doesn't need to change when a second email
    type is wired up) but is currently unused by the renderer, which only
    knows how to build the ``order_confirmation_ticket`` shell.
    """

    template_type: str = Field(default=EmailTemplateType.ORDER_CONFIRMATION_TICKET.value)
    language: str = Field(min_length=2, max_length=10)
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=50_000)

    @field_validator("language")
    @classmethod
    def _validate_language(cls, value: str) -> str:
        normalized = value.lower()
        if normalized not in SUPPORTED_LOCALES:
            raise ValueError(f"Unsupported language: {value!r}. Supported: {sorted(SUPPORTED_LOCALES)}.")
        return normalized

    @field_validator("template_type")
    @classmethod
    def _validate_template_type(cls, value: str) -> str:
        if value not in _SUPPORTED_TEMPLATE_TYPES:
            raise ValueError(f"Unsupported template_type: {value!r}. Supported: {sorted(_SUPPORTED_TEMPLATE_TYPES)}.")
        return value


class EmailTemplatePreviewResponse(BaseModel):
    """Response of the preview endpoint: the fully rendered subject/HTML/
    plain-text content, using sample (not real buyer) placeholder data."""

    subject: str
    html_body: str
    text_body: str

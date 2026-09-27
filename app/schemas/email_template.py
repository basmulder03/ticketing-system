"""Request/response models for ``EmailTemplate`` CRUD and its preview endpoint."""

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.i18n import SUPPORTED_LOCALES
from app.models.enums import EmailTemplateType

_SUPPORTED_TEMPLATE_TYPES = {t.value for t in EmailTemplateType}


class EmailTemplateUpsertRequest(BaseModel):
    """Both fields required: a template is only meaningful with subject and body."""

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
    """Render unsaved values against sample data (nothing is persisted).
    ``template_type`` is accepted but not yet used by the renderer.
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
    """Rendered subject/HTML/plain text using sample placeholder data."""

    subject: str
    html_body: str
    text_body: str

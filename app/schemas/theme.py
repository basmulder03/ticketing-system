"""Pydantic request/response models for Theme CRUD, image upload, and the
live-preview endpoint.
"""

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import PublishStatus, ThemeFont

_HEX_COLOR_PATTERN = r"^#[0-9a-fA-F]{6}$"


class ContrastPairOut(BaseModel):
    """One foreground/background contrast check result. See
    ``app.services.contrast.ContrastPairResult``."""

    label: str
    foreground: str
    background: str
    ratio: float
    passes_normal_text: bool
    passes_large_text: bool


class ContrastReportOut(BaseModel):
    """The full AA contrast report for a theme's fixed color fields. See
    ``app.services.contrast.ThemeContrastReport``.

    This report is computed only from ``primary_color``/``secondary_color``/
    ``accent_color`` — it says nothing about ``custom_css``, which cannot be
    reliably auto-audited; see ``ThemeOut.is_custom_css_active``.
    """

    pairs: list[ContrastPairOut]
    all_pass_normal_text: bool
    all_pass_large_text: bool


class ThemeUpdateRequest(BaseModel):
    """Body of ``PUT /api/v1/events/{event_id}/theme``.

    Upserts the event's theme (creates it if it doesn't exist yet). Only
    fields explicitly present in the request body are applied
    (``exclude_unset`` semantics), same convention as
    ``EventConfigUpdateRequest``. ``custom_css`` is sanitized server-side
    (see ``app.core.css_sanitizer.sanitize_custom_css``) before being
    stored — the stored/returned value is the sanitized result, never the
    raw submitted text. Logo/background images are set via the separate
    multipart upload endpoints, not this body.
    """

    primary_color: str | None = Field(default=None, pattern=_HEX_COLOR_PATTERN)
    secondary_color: str | None = Field(default=None, pattern=_HEX_COLOR_PATTERN)
    accent_color: str | None = Field(default=None, pattern=_HEX_COLOR_PATTERN)
    font_choice: ThemeFont | None = None
    custom_css: str | None = None
    status: PublishStatus | None = None


class ThemeOut(BaseModel):
    """Response shape for a Theme."""

    id: str
    event_id: str
    primary_color: str
    secondary_color: str
    accent_color: str
    logo_url: str | None
    background_image_url: str | None
    font_choice: ThemeFont
    custom_css: str | None
    is_custom_css_active: bool = Field(
        description=(
            "True if this theme has non-empty sanitized custom CSS. Since custom "
            "CSS cannot be reliably auto-audited for AA contrast, the backoffice "
            "UI should show a warning banner whenever this is true — see "
            "PROJECT_BRIEF.md's Event & Theming section."
        )
    )
    status: PublishStatus
    contrast_report: ContrastReportOut
    created_at: datetime
    updated_at: datetime


class ThemePreviewRequest(BaseModel):
    """Body of ``POST /api/v1/events/{event_id}/theme/preview``.

    All fields required (unlike ``ThemeUpdateRequest``'s partial-update
    semantics): a preview renders a complete, self-contained draft theme,
    not a delta against whatever is currently saved. ``custom_css`` is
    optional (an empty/omitted value previews with no custom CSS at all).
    """

    primary_color: str = Field(pattern=_HEX_COLOR_PATTERN)
    secondary_color: str = Field(pattern=_HEX_COLOR_PATTERN)
    accent_color: str = Field(pattern=_HEX_COLOR_PATTERN)
    font_choice: ThemeFont = ThemeFont.SYSTEM_SANS
    custom_css: str | None = None


class ThemePreviewResponse(BaseModel):
    """Response of ``POST /api/v1/events/{event_id}/theme/preview``.

    ``preview_css`` (base variables/layout for the fixed fields plus the
    sanitized custom CSS appended) and ``sample_html`` (a minimal
    ``.event-content``-scoped content block) together are enough for
    `frontend-theming` to render a live preview pane by dropping
    ``preview_css`` into a ``<style>`` tag and ``sample_html`` into the
    page — no server-rendered template involved at this stage.
    """

    sanitized_custom_css: str
    is_custom_css_active: bool
    contrast_report: ContrastReportOut
    preview_css: str
    sample_html: str

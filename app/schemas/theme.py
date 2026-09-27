"""Request/response models for Theme CRUD, image upload and live preview."""

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import PublishStatus, ThemeFont

_HEX_COLOR_PATTERN = r"^#[0-9a-fA-F]{6}$"


class ContrastPairOut(BaseModel):
    """One foreground/background contrast result."""

    label: str
    foreground: str
    background: str
    ratio: float
    passes_normal_text: bool
    passes_large_text: bool


class ContrastReportOut(BaseModel):
    """AA contrast report for the three fixed colors only — custom CSS can't be
    audited automatically (see ``ThemeOut.is_custom_css_active``).
    """

    pairs: list[ContrastPairOut]
    all_pass_normal_text: bool
    all_pass_large_text: bool


class ThemeUpdateRequest(BaseModel):
    """Upsert body; omitted fields stay unchanged. ``custom_css`` is stored and
    returned sanitized, never raw. Images use the separate upload endpoints.
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
    """A complete draft theme to preview (not a delta against the saved one);
    ``custom_css`` may be empty.
    """

    primary_color: str = Field(pattern=_HEX_COLOR_PATTERN)
    secondary_color: str = Field(pattern=_HEX_COLOR_PATTERN)
    accent_color: str = Field(pattern=_HEX_COLOR_PATTERN)
    font_choice: ThemeFont = ThemeFont.SYSTEM_SANS
    custom_css: str | None = None


class ContrastSuggestionOut(BaseModel):
    """The "closest compliant color" for one failing contrast pair."""

    field_name: str
    suggested_color: str


class ThemePreviewResponse(BaseModel):
    """Drop ``preview_css`` into a ``<style>`` and ``sample_html`` into the page to
    render the preview. ``contrast_suggestions`` is keyed by pair label and only
    has entries for failing pairs.
    """

    sanitized_custom_css: str
    is_custom_css_active: bool
    contrast_report: ContrastReportOut
    contrast_suggestions: dict[str, ContrastSuggestionOut]
    preview_css: str
    sample_html: str

"""Backoffice live theme preview: a ``<style>`` block plus a small sample
HTML block for draft (unsaved) values, not a full page render. Draft custom
CSS goes through the same sanitizer as saving.
"""

from dataclasses import dataclass

from app.core.css_sanitizer import EVENT_CONTENT_CLASS, sanitize_custom_css
from app.models.enums import ThemeFont
from app.services.color import nudge_lightness_for_contrast
from app.services.contrast import (
    AA_NORMAL_TEXT_THRESHOLD,
    ThemeContrastReport,
    check_theme_contrast,
)

FONT_STACKS: dict[ThemeFont, str] = {
    ThemeFont.SYSTEM_SANS: "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
    ThemeFont.SYSTEM_SERIF: "Georgia, 'Times New Roman', Times, serif",
    ThemeFont.INTER: "'Inter', system-ui, sans-serif",
    ThemeFont.ROBOTO: "'Roboto', system-ui, sans-serif",
    ThemeFont.OPEN_SANS: "'Open Sans', system-ui, sans-serif",
    ThemeFont.LORA: "'Lora', Georgia, serif",
    ThemeFont.MERRIWEATHER: "'Merriweather', Georgia, serif",
    ThemeFont.PLAYFAIR_DISPLAY: "'Playfair Display', Georgia, serif",
}
"""``font-family`` stack per ``ThemeFont``. The named fonts are self-hosted
(``app/static/fonts.css``); each fallback is only what renders before that
font file loads, or if it somehow fails to.
"""


@dataclass(frozen=True)
class ContrastSuggestion:
    """A "closest compliant color" for one failing pair: the same hue and
    saturation, lightness nudged until it clears 4.5:1. ``field_name`` tells the
    template which color input the "use this color" button fills.
    """

    field_name: str
    suggested_color: str


def _build_contrast_suggestions(
    report: ThemeContrastReport, *, primary_color: str, secondary_color: str, accent_color: str
) -> dict[str, ContrastSuggestion]:
    """One suggestion per failing pair, keyed by the pair's label.

    Always adjust the color unique to each pair (``primary`` for the first,
    ``accent`` for the second), never the shared ``secondary``: it's the
    background in one pair and the text in the other, so changing it could
    break the pair that already passes. Against a fixed background either
    black or white always clears 4.5:1 (their ratios multiply to 21), so every
    suggestion can be applied together and the whole report ends up passing.
    """
    suggestions: dict[str, ContrastSuggestion] = {}
    for pair in report.pairs:
        if pair.passes_normal_text:
            continue
        if pair.label == "primary vs secondary":
            field_name, movable_color, anchor_color = "primary_color", primary_color, secondary_color
        elif pair.label == "secondary vs accent":
            field_name, movable_color, anchor_color = "accent_color", accent_color, secondary_color
        else:
            continue
        suggested = nudge_lightness_for_contrast(movable_color, anchor_color, target_ratio=AA_NORMAL_TEXT_THRESHOLD)
        suggestions[pair.label] = ContrastSuggestion(field_name=field_name, suggested_color=suggested)
    return suggestions


@dataclass(frozen=True)
class ThemePreviewResult:
    """Everything needed to render the preview pane."""

    sanitized_custom_css: str
    is_custom_css_active: bool
    contrast_report: ThemeContrastReport
    contrast_suggestions: dict[str, ContrastSuggestion]
    preview_css: str
    sample_html: str


def build_theme_preview(
    *,
    primary_color: str,
    secondary_color: str,
    accent_color: str,
    font_choice: ThemeFont,
    custom_css: str | None,
) -> ThemePreviewResult:
    """Build the preview from draft theme values. Pure — no DB access."""
    sanitized_css = sanitize_custom_css(custom_css or "")
    contrast_report = check_theme_contrast(
        primary_color=primary_color, secondary_color=secondary_color, accent_color=accent_color
    )
    contrast_suggestions = _build_contrast_suggestions(
        contrast_report, primary_color=primary_color, secondary_color=secondary_color, accent_color=accent_color
    )
    font_stack = FONT_STACKS[font_choice]

    base_css = (
        f".{EVENT_CONTENT_CLASS} {{\n"
        f"  --beacon-color-primary: {primary_color};\n"
        f"  --beacon-color-secondary: {secondary_color};\n"
        f"  --beacon-color-accent: {accent_color};\n"
        f"  font-family: {font_stack};\n"
        f"  background: var(--beacon-color-secondary);\n"
        f"  color: var(--beacon-color-primary);\n"
        f"  padding: 1.5rem;\n"
        f"}}\n"
        f".{EVENT_CONTENT_CLASS} .beacon-preview-cta {{\n"
        f"  background: var(--beacon-color-accent);\n"
        f"  color: var(--beacon-color-secondary);\n"
        f"  border: none;\n"
        f"  padding: 0.75rem 1.5rem;\n"
        f"  font: inherit;\n"
        f"}}\n"
    )
    preview_css = base_css if not sanitized_css else f"{base_css}\n{sanitized_css}\n"

    sample_html = (
        f'<div class="{EVENT_CONTENT_CLASS}">\n'
        f"  <h1>Sample Event Title</h1>\n"
        f"  <p>Doors open 19:30, show starts 20:00 — Sample Venue.</p>\n"
        f'  <button type="button" class="beacon-preview-cta">Buy tickets</button>\n'
        f"</div>\n"
    )

    return ThemePreviewResult(
        sanitized_custom_css=sanitized_css,
        is_custom_css_active=bool(sanitized_css),
        contrast_report=contrast_report,
        contrast_suggestions=contrast_suggestions,
        preview_css=preview_css,
        sample_html=sample_html,
    )

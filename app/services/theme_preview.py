"""Builds the response for the Theme "live preview" endpoint.

Per PROJECT_BRIEF.md's Event & Theming section ("Live theme preview in
backoffice before publishing"), `frontend-theming` needs something to
render a preview pane against draft (not-yet-saved) theme values —
including draft custom CSS, which MUST go through the exact same sanitizer
as the real save path (see ``app.core.css_sanitizer.sanitize_custom_css``;
this module never calls a relaxed/"preview-only" variant).

Deliberately minimal per the brief ("no need to fully render the eventual
public landing page... a minimal sample content block is enough"): this
returns a small, self-contained ``<style>`` block plus a small HTML sample
block, not a full page render. Full landing-page theming is Milestone 2 /
`frontend-theming` scope.
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
"""CSS ``font-family`` fallback stack per curated ``ThemeFont`` choice. The
non-system entries name a specific web font family but rely on the browser
falling back to a system font unless/until ``frontend-theming`` actually
self-hosts (``@font-face``) that family in a later milestone — this module
never itself loads a font from a third-party URL, keeping the same
no-external-request posture the CSS sanitizer enforces for custom CSS."""


@dataclass(frozen=True)
class ContrastSuggestion:
    """A "closest compliant color" suggestion for one failing contrast
    pair (post-launch fix, per the user's NOTES: "Do color recommendations
    for what color can be used ... with easy setting of that color").

    ``field_name`` is which of the Theme's three form fields this
    suggestion applies to (``"primary_color"``/``"secondary_color"``/
    ``"accent_color"``) — the backoffice template uses it to wire a
    one-click "use this color" button straight to the right ``<input
    type="color">``. ``suggested_color`` always targets the stricter
    4.5:1 normal-text threshold (clearing it also clears the 3:1 large-
    text/UI threshold every pair is also checked against), computed by
    :func:`app.services.color.nudge_lightness_for_contrast` -- the same
    hue/saturation-preserving search :func:`app.services.color.
    derive_dark_palette` already uses, so a suggestion always reads as
    "your color, nudged" rather than an unrelated replacement.
    """

    field_name: str
    suggested_color: str


def _build_contrast_suggestions(
    report: ThemeContrastReport, *, primary_color: str, secondary_color: str, accent_color: str
) -> dict[str, ContrastSuggestion]:
    """One suggestion per FAILING pair in ``report``, keyed by
    ``ContrastPairResult.label`` -- a pair that already passes needs no
    suggestion, and gets no entry here (the template checks for that key's
    presence rather than a sentinel value).

    Which field gets adjusted is hardcoded per pair below, deliberately
    NEVER ``secondary_color`` -- even though ``check_theme_contrast``
    narrowed its checks to just 2 pairs specifically so they'd be
    independently satisfiable (see that function's docstring), the two
    pairs still share ``secondary_color``, just in different ROLES: it's
    the BACKGROUND in "primary vs secondary" but the FOREGROUND in
    "secondary vs accent" (the button-label color). Suggesting a fix to
    whichever field happens to be a pair's ``foreground`` -- the first,
    more general approach here -- would sometimes suggest adjusting
    ``secondary_color`` to fix "secondary vs accent", which can silently
    un-satisfy "primary vs secondary" the moment it's applied (found live,
    the exact "fixing one part breaks another" complaint this whole
    narrower 2-pair check was built to eliminate). Always adjusting the
    field that appears in ONLY ONE of the two real pairs instead
    (``primary_color`` for the first, ``accent_color`` for the second)
    keeps ``secondary_color`` as a fixed anchor both other colors are
    chosen relative to -- which is always achievable, since for any fixed
    background, either black or white clears 4.5:1 against it (their
    contrast ratios multiply to a constant 21, so at least one always
    reaches >= sqrt(21) ~= 4.58) -- guaranteeing every suggestion offered
    here can be applied simultaneously and leave the whole report passing.
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
    """Everything `frontend-theming` needs to render a live preview pane."""

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
    """Build a :class:`ThemePreviewResult` from draft (unsaved) theme field
    values. Pure function of its arguments — no DB access — so it can be
    called identically from the preview endpoint (draft values) and could,
    if ever useful, be called against an already-persisted Theme's values
    too.
    """
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

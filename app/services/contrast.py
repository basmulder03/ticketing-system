"""WCAG 2.1 AA contrast-ratio checking for a Theme's fixed color fields.

Per PROJECT_BRIEF.md's Event & Theming section: "Automated AA contrast
check against the fixed theme fields; show a warning banner in backoffice
if custom CSS is active (since arbitrary CSS can't be reliably
auto-audited)". This module only ever looks at the three fixed hex colors
(``primary``/``secondary``/``accent``) — it cannot and does not attempt to
audit ``custom_css``; the ``is_custom_css_active`` signal exposed alongside
it (see ``app.schemas.theme.ThemeOut``) is what tells the backoffice UI to
show that warning banner instead.

Pure, deterministic, DB-free — callable directly by the Theme routes and by
the preview endpoint against not-yet-saved values, and easy for
``test-writer`` to hit with known-good/known-bad color pairs.
"""

from dataclasses import dataclass

_HEX_LEN = 7  # "#rrggbb"

AA_NORMAL_TEXT_THRESHOLD = 4.5
"""WCAG 2.1 AA minimum contrast ratio for normal-size text."""

AA_LARGE_TEXT_THRESHOLD = 3.0
"""WCAG 2.1 AA minimum contrast ratio for large-scale text (>=18pt, or
>=14pt bold) and for UI-component/graphical-object contrast."""

WHITE = "#ffffff"
BLACK = "#000000"


@dataclass(frozen=True)
class ContrastPairResult:
    """The computed contrast ratio for one foreground/background pairing,
    plus pass/fail against both AA thresholds — a structured result (not
    just a bool) so the backoffice UI can show the actual numbers, per the
    brief's "flag which pairs fail" requirement."""

    label: str
    foreground: str
    background: str
    ratio: float
    passes_normal_text: bool
    passes_large_text: bool


@dataclass(frozen=True)
class ThemeContrastReport:
    """The full set of contrast checks run against one theme's fixed colors."""

    pairs: list[ContrastPairResult]

    @property
    def all_pass_normal_text(self) -> bool:
        """True only if every checked pair clears the 4.5:1 normal-text threshold."""
        return all(p.passes_normal_text for p in self.pairs)

    @property
    def all_pass_large_text(self) -> bool:
        """True only if every checked pair clears the 3:1 large-text/UI threshold."""
        return all(p.passes_large_text for p in self.pairs)


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """Parse a ``#rrggbb`` string into an (r, g, b) tuple of 0-255 ints.

    Raises ``ValueError`` for anything not shaped like a 6-digit hex color —
    callers are expected to have already validated the shape at the schema
    layer (see ``app.schemas.theme``); this is a defensive second check,
    not the primary validation point.
    """
    if len(hex_color) != _HEX_LEN or not hex_color.startswith("#"):
        raise ValueError(f"Not a #rrggbb hex color: {hex_color!r}")
    return (
        int(hex_color[1:3], 16),
        int(hex_color[3:5], 16),
        int(hex_color[5:7], 16),
    )


def _channel_to_linear(channel_8bit: int) -> float:
    """Convert one sRGB 0-255 channel value to its linear-light value, per
    the WCAG 2.1 relative luminance formula."""
    channel = channel_8bit / 255.0
    if channel <= 0.03928:
        return channel / 12.92
    return float(((channel + 0.055) / 1.055) ** 2.4)


def relative_luminance(hex_color: str) -> float:
    """Compute the WCAG relative luminance (0.0-1.0) of a ``#rrggbb`` color."""
    r, g, b = _hex_to_rgb(hex_color)
    r_lin, g_lin, b_lin = _channel_to_linear(r), _channel_to_linear(g), _channel_to_linear(b)
    return 0.2126 * r_lin + 0.7152 * g_lin + 0.0722 * b_lin


def contrast_ratio(color_a: str, color_b: str) -> float:
    """Compute the WCAG contrast ratio between two ``#rrggbb`` colors.

    The formula is symmetric — which color is "foreground" vs "background"
    does not affect the ratio — per the WCAG 2.1 definition:
    ``(L1 + 0.05) / (L2 + 0.05)`` where L1 is the lighter of the two
    relative luminances.
    """
    lum_a = relative_luminance(color_a)
    lum_b = relative_luminance(color_b)
    lighter, darker = max(lum_a, lum_b), min(lum_a, lum_b)
    return (lighter + 0.05) / (darker + 0.05)


def _check_pair(label: str, foreground: str, background: str) -> ContrastPairResult:
    ratio = contrast_ratio(foreground, background)
    return ContrastPairResult(
        label=label,
        foreground=foreground,
        background=background,
        ratio=round(ratio, 2),
        passes_normal_text=ratio >= AA_NORMAL_TEXT_THRESHOLD,
        passes_large_text=ratio >= AA_LARGE_TEXT_THRESHOLD,
    )


def check_theme_contrast(*, primary_color: str, secondary_color: str, accent_color: str) -> ThemeContrastReport:
    """Run the AA contrast check against a theme's three fixed colors.

    Pair selection (documented judgement call, since the brief names only
    the three color fields without prescribing which combinations matter):
    a real theme built from these three colors will realistically use each
    one as a background behind either white or black text/icons (the two
    most common text colors paired against an arbitrary brand color, e.g.
    a colored header bar or button), so each of the three colors is checked
    against both white and black (6 pairs). The three colors are also
    realistically used directly against each other (e.g. accent-colored
    text/links on a primary-colored section, or a secondary-colored panel
    on a primary-colored page background), so the three unordered pairs
    among them are checked too (3 pairs) — the ratio is symmetric, so
    "foreground" vs "background" labeling there is illustrative, not
    order-significant. Total: 9 pairs.

    This intentionally does NOT and cannot check ``custom_css`` — see this
    module's docstring.
    """
    pairs = [
        _check_pair("primary on white", primary_color, WHITE),
        _check_pair("primary on black", primary_color, BLACK),
        _check_pair("secondary on white", secondary_color, WHITE),
        _check_pair("secondary on black", secondary_color, BLACK),
        _check_pair("accent on white", accent_color, WHITE),
        _check_pair("accent on black", accent_color, BLACK),
        _check_pair("primary vs secondary", primary_color, secondary_color),
        _check_pair("primary vs accent", primary_color, accent_color),
        _check_pair("secondary vs accent", secondary_color, accent_color),
    ]
    return ThemeContrastReport(pairs=pairs)

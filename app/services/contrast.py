"""WCAG 2.1 AA contrast checks for a Theme's three fixed colors. Custom CSS
can't be audited; the backoffice shows a warning when it's active. Pure.
"""

from dataclasses import dataclass

_HEX_LEN = 7  # "#rrggbb"

AA_NORMAL_TEXT_THRESHOLD = 4.5
"""WCAG 2.1 AA minimum contrast ratio for normal-size text."""

AA_LARGE_TEXT_THRESHOLD = 3.0
"""AA minimum for large text (>=18pt, or >=14pt bold) and UI components."""


@dataclass(frozen=True)
class ContrastPairResult:
    """Ratio plus pass/fail for one pair, so the UI can show the numbers."""

    label: str
    foreground: str
    background: str
    ratio: float
    passes_normal_text: bool
    passes_large_text: bool


@dataclass(frozen=True)
class ThemeContrastReport:
    """All contrast checks for one theme."""

    pairs: list[ContrastPairResult]

    @property
    def all_pass_normal_text(self) -> bool:
        """Every pair clears 4.5:1."""
        return all(p.passes_normal_text for p in self.pairs)

    @property
    def all_pass_large_text(self) -> bool:
        """Every pair clears 3:1."""
        return all(p.passes_large_text for p in self.pairs)


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """``#rrggbb`` → (r, g, b). Raises ``ValueError``; schemas validate first."""
    if len(hex_color) != _HEX_LEN or not hex_color.startswith("#"):
        raise ValueError(f"Not a #rrggbb hex color: {hex_color!r}")
    return (
        int(hex_color[1:3], 16),
        int(hex_color[3:5], 16),
        int(hex_color[5:7], 16),
    )


def _channel_to_linear(channel_8bit: int) -> float:
    """sRGB channel → linear light (WCAG formula)."""
    channel = channel_8bit / 255.0
    if channel <= 0.03928:
        return channel / 12.92
    return float(((channel + 0.055) / 1.055) ** 2.4)


def relative_luminance(hex_color: str) -> float:
    """WCAG relative luminance (0-1)."""
    r, g, b = _hex_to_rgb(hex_color)
    r_lin, g_lin, b_lin = _channel_to_linear(r), _channel_to_linear(g), _channel_to_linear(b)
    return 0.2126 * r_lin + 0.7152 * g_lin + 0.0722 * b_lin


def contrast_ratio(color_a: str, color_b: str) -> float:
    """WCAG contrast ratio; symmetric in its arguments."""
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
    """Check the only two pairings the app actually renders text on:

    1. primary on secondary — body text everywhere (page, emails, PDFs).
    2. secondary on accent — the primary button label.

    Don't add pairs like "vs white/black": no real pixel uses them, and no
    color can pass against both white *and* black, so the report could never
    go green. Because ``secondary`` is the background in both pairs, fixing
    ``primary`` or ``accent`` never affects the other pair — applying every
    suggestion always reaches full compliance.
    """
    pairs = [
        _check_pair("primary vs secondary", primary_color, secondary_color),
        _check_pair("secondary vs accent", secondary_color, accent_color),
    ]
    return ThemeContrastReport(pairs=pairs)

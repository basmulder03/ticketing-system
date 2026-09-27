"""HSL helpers for adjusting a theme color's lightness until it meets a
contrast target while keeping its hue and saturation. Used by the automatic
dark-mode palette and the theme editor's "closest compliant color"
suggestions. ``app.services.contrast.contrast_ratio`` decides what passes.
"""

from app.services.contrast import contrast_ratio

__all__ = ["derive_dark_palette", "hex_to_hsl", "hsl_to_hex", "nudge_lightness_for_contrast"]

_HEX_LEN = 7
_LIGHTNESS_STEP = 0.02
_MAX_STEPS = 49  # enough to walk from 0.0 to ~1.0 in _LIGHTNESS_STEP increments


def _hex_to_rgb01(hex_color: str) -> tuple[float, float, float]:
    if len(hex_color) != _HEX_LEN or not hex_color.startswith("#"):
        raise ValueError(f"Not a #rrggbb hex color: {hex_color!r}")
    return (
        int(hex_color[1:3], 16) / 255.0,
        int(hex_color[3:5], 16) / 255.0,
        int(hex_color[5:7], 16) / 255.0,
    )


def hex_to_hsl(hex_color: str) -> tuple[float, float, float]:
    """``#rrggbb`` → (hue degrees [0, 360), saturation, lightness in [0, 1])."""
    r, g, b = _hex_to_rgb01(hex_color)
    channel_max, channel_min = max(r, g, b), min(r, g, b)
    lightness = (channel_max + channel_min) / 2
    delta = channel_max - channel_min

    if delta == 0:
        return 0.0, 0.0, lightness

    saturation = delta / (1 - abs(2 * lightness - 1))

    if channel_max == r:
        hue = 60 * (((g - b) / delta) % 6)
    elif channel_max == g:
        hue = 60 * ((b - r) / delta + 2)
    else:
        hue = 60 * ((r - g) / delta + 4)

    return hue % 360, saturation, lightness


def hsl_to_hex(hue: float, saturation: float, lightness: float) -> str:
    """Inverse of :func:`hex_to_hsl`; out-of-range inputs are clamped."""
    hue = hue % 360
    saturation = max(0.0, min(1.0, saturation))
    lightness = max(0.0, min(1.0, lightness))

    chroma = (1 - abs(2 * lightness - 1)) * saturation
    x = chroma * (1 - abs((hue / 60) % 2 - 1))
    m = lightness - chroma / 2

    if hue < 60:
        r, g, b = chroma, x, 0.0
    elif hue < 120:
        r, g, b = x, chroma, 0.0
    elif hue < 180:
        r, g, b = 0.0, chroma, x
    elif hue < 240:
        r, g, b = 0.0, x, chroma
    elif hue < 300:
        r, g, b = x, 0.0, chroma
    else:
        r, g, b = chroma, 0.0, x

    def to_255(component: float) -> int:
        return round(max(0.0, min(1.0, component + m)) * 255)

    return f"#{to_255(r):02x}{to_255(g):02x}{to_255(b):02x}"


def nudge_lightness_for_contrast(
    hex_color: str, background_hex: str, *, target_ratio: float, prefer_direction: str | None = None
) -> str:
    """Same hue/saturation, lightness stepped until ``target_ratio`` is met
    against ``background_hex`` (or the most contrasting extreme if it can't be).

    Hue is kept so the result still reads as the admin's color. The direction
    is picked automatically unless ``prefer_direction`` forces it (dark mode
    forces ``"lighter"`` so text never sinks into a dark background).
    """
    hue, saturation, lightness = hex_to_hsl(hex_color)

    if contrast_ratio(hex_color, background_hex) >= target_ratio and prefer_direction is None:
        return hex_color

    if prefer_direction in ("lighter", "darker"):
        directions = [1 if prefer_direction == "lighter" else -1]
    else:
        # Compare the reachable *extremes* in each direction, not one step: near
        # a lightness boundary (e.g. white on off-white) one step can briefly lose
        # contrast before gaining it, picking a direction with nowhere to go.
        # Both directions are still tried in ranked order.
        lightest_ratio = contrast_ratio(hsl_to_hex(hue, saturation, 1.0), background_hex)
        darkest_ratio = contrast_ratio(hsl_to_hex(hue, saturation, 0.0), background_hex)
        directions = [1, -1] if lightest_ratio >= darkest_ratio else [-1, 1]

    best_hex = hex_color
    best_ratio = contrast_ratio(hex_color, background_hex)
    for direction in directions:
        current_lightness = lightness
        for _ in range(_MAX_STEPS):
            current_lightness = max(0.0, min(1.0, current_lightness + direction * _LIGHTNESS_STEP))
            candidate_hex = hsl_to_hex(hue, saturation, current_lightness)
            candidate_ratio = contrast_ratio(candidate_hex, background_hex)
            if candidate_ratio > best_ratio:
                best_hex, best_ratio = candidate_hex, candidate_ratio
            if candidate_ratio >= target_ratio:
                return candidate_hex
            if current_lightness in (0.0, 1.0):
                break
        if best_ratio >= target_ratio:
            break

    return best_hex


def derive_dark_palette(*, primary_color: str, secondary_color: str, accent_color: str) -> dict[str, str]:
    """Dark-mode variant of a theme's colors, computed at render time (nothing
    stored, so it always follows the current theme).

    - Background: primary's hue, desaturated, very dark — keeps brand identity
      instead of inverting a near-white secondary into plain black.
    - Text: secondary's hue, light, guaranteed to clear 4.5:1 on that background.
    - Accent: unchanged if it already clears 3:1; otherwise lightened.
    """
    primary_hue, _primary_sat, _primary_light = hex_to_hsl(primary_color)
    dark_background = hsl_to_hex(primary_hue, 0.28, 0.12)

    secondary_hue, secondary_sat, _secondary_light = hex_to_hsl(secondary_color)
    light_text_candidate = hsl_to_hex(secondary_hue, min(secondary_sat, 0.45), 0.92)
    dark_text = nudge_lightness_for_contrast(
        light_text_candidate, dark_background, target_ratio=4.5, prefer_direction="lighter"
    )

    dark_accent = accent_color
    if contrast_ratio(accent_color, dark_background) < 3.0:
        dark_accent = nudge_lightness_for_contrast(
            accent_color, dark_background, target_ratio=3.0, prefer_direction="lighter"
        )

    return {
        "background": dark_background,
        "text": dark_text,
        "accent": dark_accent,
    }

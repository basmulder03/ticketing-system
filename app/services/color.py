"""HSL color-space utilities for deriving accessible color variants from an
admin-chosen Theme color, without picking a new hue out of thin air.

Two features on the post-Milestone-9 follow-up list need exactly the same
underlying operation — "nudge this color's lightness until it clears a
contrast target against some background, staying as close to the original
hue/saturation as possible" — so it lives here once, not duplicated:

1. **Auto-computed dark mode** (:func:`derive_dark_palette`): a dark-mode
   variant of an Event's Theme, computed at render time so a buyer's system
   dark-mode preference is honored without the admin doing anything extra,
   while staying recognizably "the same brand" rather than a generic
   inverted palette.
2. A future "closest compliant color" suggestion in the backoffice theme
   editor (not built yet — this module is written to make that a thin
   wrapper around :func:`nudge_lightness_for_contrast` when it lands, not a
   reason to duplicate the HSL math again).

Reuses ``app.services.contrast.contrast_ratio`` (the existing WCAG
relative-luminance-based ratio, already relied on by the theme editor's own
AA check) as the single source of truth for "does this pass" — this module
only adds the HSL conversions and the search loop, never redefines what a
passing ratio means.
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
    """Convert a ``#rrggbb`` color to ``(hue, saturation, lightness)``, hue
    in degrees ``[0, 360)``, saturation/lightness as fractions ``[0, 1]`` —
    the standard HSL cylindrical-coordinate transform of sRGB."""
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
    """Convert ``(hue, saturation, lightness)`` (degrees / fraction /
    fraction) back to a ``#rrggbb`` string — the inverse of :func:`hex_to_hsl`.
    Inputs are clamped to their valid ranges first, so a caller that walked
    lightness slightly out of ``[0, 1]`` while searching can't produce an
    invalid color."""
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
    """Return a color with the SAME hue/saturation as ``hex_color`` but with
    its lightness stepped toward whichever direction (lighter/darker)
    increases contrast against ``background_hex``, stopping as soon as
    ``target_ratio`` is met — or, if it can't be reached at all (a very
    low-saturation near-gray input trying to hit an unreasonably high
    target), returning the most-contrasting extreme it found (pure black or
    white would always pass, so this only happens for a target above ~21:1,
    not a realistic AA/AAA threshold).

    Deliberately preserves hue/saturation rather than searching in full RGB
    space: a "fix the contrast" suggestion that also shifts the color's hue
    would not read as "the same brand color, adjusted" to the person who
    picked it — this stays recognizably the same color, just lighter or
    darker.

    ``prefer_direction`` (``"lighter"``/``"darker"``/``None``) overrides
    which way to search when the color's current lightness already clears
    the target in principle from either direction (rare, but possible near
    the midtones) — used by :func:`derive_dark_palette` to force a
    consistent "make it lighter" search for dark-mode text colors rather
    than risk it nudging toward an even-darker near-black that would blend
    into a dark background instead of standing out from it. If ``None``,
    the direction is chosen automatically as whichever one currently has
    higher contrast against the background.
    """
    hue, saturation, lightness = hex_to_hsl(hex_color)

    if contrast_ratio(hex_color, background_hex) >= target_ratio and prefer_direction is None:
        return hex_color

    if prefer_direction in ("lighter", "darker"):
        direction = 1 if prefer_direction == "lighter" else -1
    else:
        lighter_candidate = hsl_to_hex(hue, saturation, min(1.0, lightness + _LIGHTNESS_STEP))
        darker_candidate = hsl_to_hex(hue, saturation, max(0.0, lightness - _LIGHTNESS_STEP))
        lighter_ratio = contrast_ratio(lighter_candidate, background_hex)
        darker_ratio = contrast_ratio(darker_candidate, background_hex)
        direction = 1 if lighter_ratio >= darker_ratio else -1

    best_hex = hex_color
    best_ratio = contrast_ratio(hex_color, background_hex)
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

    return best_hex


def derive_dark_palette(*, primary_color: str, secondary_color: str, accent_color: str) -> dict[str, str]:
    """Derive a dark-mode variant of a Theme's three fixed colors, for use
    under ``@media (prefers-color-scheme: dark)`` — computed at render time,
    not stored, so it always reflects the Theme's current colors with no
    separate "dark mode colors" fields for an admin to keep in sync.

    Design, not an arbitrary inversion:

    - **Background** takes ``primary_color``'s hue (usually the more
      "branded"/saturated of the two fixed background-ish colors — real
      themes tend to pick a bold primary and a plain/white-ish secondary,
      so anchoring the dark background's hue to primary keeps it
      recognizably "this event's colors" rather than a generic charcoal),
      desaturated somewhat (dark UI surfaces read as muddy at full
      saturation) and forced to a low, fixed lightness band — not just
      "invert secondary's lightness", since a light theme's secondary is
      often pure/near-white, whose naive inversion is pure/near-black with
      no color identity left in it at all.
    - **Text** (the role ``secondary_color`` — er, ``primary_color`` plays
      against a ``secondary`` background in light mode) takes
      ``secondary_color``'s hue at a HIGH lightness, then
      :func:`nudge_lightness_for_contrast` (forced ``"lighter"``) guarantees
      it clears AA normal-text contrast (4.5:1) against the new dark
      background — this is the one color guaranteed correct by
      construction, not just by luck.
    - **Accent** keeps ``accent_color`` completely unchanged if it already
      clears AA large-text/UI contrast (3:1) against the new dark
      background (a saturated accent color often already reads fine on a
      dark surface) — only nudged lighter if it doesn't, preserving the
      admin's exact chosen accent whenever possible rather than always
      recoloring it.
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

"""Unit tests for ``app.services.color``: the HSL round-trip math and the
dark-mode palette derivation built on top of it.

Pure, deterministic, no DB/app context needed — same discipline as
``tests/unit/test_contrast.py``, which this module's ``contrast_ratio``
reuse depends on.
"""

import pytest

from app.services.color import (
    derive_dark_palette,
    hex_to_hsl,
    hsl_to_hex,
    nudge_lightness_for_contrast,
)
from app.services.contrast import contrast_ratio

# --- hex <-> HSL round-trip ---


@pytest.mark.parametrize(
    "hex_color",
    ["#000000", "#ffffff", "#808080", "#1a1a1a", "#c9a227", "#2d5f8a", "#e63946", "#1b4332", "#ff00ff"],
)
def test_hex_to_hsl_to_hex_round_trips_exactly(hex_color: str) -> None:
    hue, saturation, lightness = hex_to_hsl(hex_color)
    assert hsl_to_hex(hue, saturation, lightness) == hex_color


def test_pure_white_has_zero_saturation_and_full_lightness() -> None:
    _hue, saturation, lightness = hex_to_hsl("#ffffff")
    assert saturation == 0.0
    assert lightness == pytest.approx(1.0)


def test_pure_black_has_zero_saturation_and_zero_lightness() -> None:
    _hue, saturation, lightness = hex_to_hsl("#000000")
    assert saturation == 0.0
    assert lightness == pytest.approx(0.0)


def test_primary_red_has_hue_zero_full_saturation_and_mid_lightness() -> None:
    hue, saturation, lightness = hex_to_hsl("#ff0000")
    assert hue == pytest.approx(0.0)
    assert saturation == pytest.approx(1.0)
    assert lightness == pytest.approx(0.5)


def test_hsl_to_hex_clamps_out_of_range_inputs_rather_than_erroring() -> None:
    # A caller that walked lightness slightly past 1.0 while searching must
    # still get back a valid color, not a crash or garbage bytes.
    assert hsl_to_hex(45.0, 1.5, 1.2) == "#ffffff"
    assert hsl_to_hex(45.0, -0.5, -0.5) == "#000000"


# --- nudge_lightness_for_contrast ---


def test_nudge_returns_the_input_unchanged_if_it_already_passes() -> None:
    # Black on white already clears any realistic target; no change needed.
    assert nudge_lightness_for_contrast("#000000", "#ffffff", target_ratio=4.5) == "#000000"


def test_nudge_lightens_a_dark_color_that_fails_against_a_dark_background() -> None:
    original = "#5c1a1a"  # dark maroon
    background = "#271616"  # dark background it would blend into
    assert contrast_ratio(original, background) < 3.0

    nudged = nudge_lightness_for_contrast(original, background, target_ratio=3.0)

    assert contrast_ratio(nudged, background) >= 3.0
    # Hue preserved (still reads as "red-ish", not recolored to something
    # unrelated) -- same hue bucket, allowing for HSL rounding at the
    # extremes.
    original_hue, _, _ = hex_to_hsl(original)
    nudged_hue, _, _ = hex_to_hsl(nudged)
    assert abs(original_hue - nudged_hue) < 5.0


def test_nudge_prefer_direction_lighter_forces_that_search_direction() -> None:
    # A midtone gray already has *some* contrast against a midtone
    # background in both directions; forcing "lighter" must never return a
    # darker-than-original result.
    original = "#808080"
    background = "#808080"
    nudged = nudge_lightness_for_contrast(original, background, target_ratio=4.5, prefer_direction="lighter")
    _, _, original_lightness = hex_to_hsl(original)
    _, _, nudged_lightness = hex_to_hsl(nudged)
    assert nudged_lightness >= original_lightness


def test_nudge_gives_up_gracefully_on_an_unreachable_target() -> None:
    # 21:1 is the theoretical maximum (pure black vs. pure white) -- no
    # non-white color can reach it against a non-black background. Must
    # return its best attempt, not raise or loop forever.
    result = nudge_lightness_for_contrast("#808080", "#808080", target_ratio=21.0)
    assert result.startswith("#") and len(result) == 7


def test_nudge_finds_a_passing_color_when_already_at_the_light_boundary() -> None:
    # A real bug found live: pure white (already at lightness=1.0, the
    # boundary) against a near-white background. A naive single-step
    # lookahead picks "lighter" (white can't get any lighter -- the step
    # is clamped to a no-op) because stepping "darker" by one unit briefly
    # LOSES contrast as lightness approaches the background's own, before
    # eventually gaining it back well past that point -- so the old
    # direction heuristic saw only that initial loss, committed to
    # "lighter", and gave up after one no-op step, silently returning the
    # input unchanged despite it failing outright (1.16:1, nowhere near
    # the 4.5:1 target). Must actually search "darker" here and find a
    # real, passing color.
    original = "#ffffff"
    background = "#eeeeee"
    assert contrast_ratio(original, background) < 4.5

    nudged = nudge_lightness_for_contrast(original, background, target_ratio=4.5)

    assert nudged != original
    assert contrast_ratio(nudged, background) >= 4.5


def test_nudge_finds_a_passing_color_when_already_at_the_dark_boundary() -> None:
    # The symmetric case: pure black against a near-black background.
    original = "#000000"
    background = "#111111"
    assert contrast_ratio(original, background) < 4.5

    nudged = nudge_lightness_for_contrast(original, background, target_ratio=4.5)

    assert nudged != original
    assert contrast_ratio(nudged, background) >= 4.5


# --- derive_dark_palette ---


@pytest.mark.parametrize(
    ("primary", "secondary", "accent"),
    [
        ("#1a1a1a", "#ffffff", "#c9a227"),  # warm gold on near-black/white
        ("#0b3d66", "#ffffff", "#e63946"),  # navy/white/red
        ("#1b4332", "#f1faee", "#e76f51"),  # forest green theme
        ("#ffffff", "#111111", "#00aaff"),  # inverted roles (light primary)
        ("#1a1a1a", "#ffffff", "#5c1a1a"),  # accent that needs nudging against the dark bg
    ],
)
def test_derive_dark_palette_always_meets_its_own_contrast_targets(
    primary: str, secondary: str, accent: str
) -> None:
    dark = derive_dark_palette(primary_color=primary, secondary_color=secondary, accent_color=accent)

    assert set(dark.keys()) == {"background", "text", "accent"}
    assert contrast_ratio(dark["text"], dark["background"]) >= 4.5
    assert contrast_ratio(dark["accent"], dark["background"]) >= 3.0


def test_derive_dark_palette_background_is_genuinely_dark() -> None:
    dark = derive_dark_palette(primary_color="#0b3d66", secondary_color="#ffffff", accent_color="#e63946")
    _, _, lightness = hex_to_hsl(dark["background"])
    assert lightness < 0.2


def test_derive_dark_palette_preserves_a_passing_accent_unchanged() -> None:
    # A saturated accent that already clears 3:1 against the derived dark
    # background should come back byte-for-byte identical -- the admin's
    # exact chosen accent, not a lookalike.
    dark = derive_dark_palette(primary_color="#1a1a1a", secondary_color="#ffffff", accent_color="#c9a227")
    assert dark["accent"] == "#c9a227"


def test_derive_dark_palette_background_hue_tracks_the_primary_color() -> None:
    # Anchoring the dark background's hue to primary (not a flat gray)
    # keeps two different themes' dark modes visually distinct from each
    # other, matching this module's stated design intent.
    blue_dark = derive_dark_palette(primary_color="#0b3d66", secondary_color="#ffffff", accent_color="#e63946")
    green_dark = derive_dark_palette(primary_color="#1b4332", secondary_color="#f1faee", accent_color="#e76f51")
    assert blue_dark["background"] != green_dark["background"]

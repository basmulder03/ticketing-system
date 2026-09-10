"""Unit tests for ``app.services.contrast``: the WCAG 2.1 AA contrast-ratio
math and pass/fail threshold logic behind Milestone 1.5's "Automated AA
contrast check against the fixed theme fields" requirement.

Pure, deterministic, no DB/app context needed. Reference ratios (black on
white ~21:1, white on white 1:1) and threshold-boundary colors are
well-established WCAG facts, verified here against this module's own
implementation of the relative-luminance/ratio formula. ``_WHITE``/
``_BLACK`` below are local test-reference colors only (module no longer
exports ``WHITE``/``BLACK`` — see the pair-selection note below).
"""

import pytest

from app.services.contrast import (
    AA_LARGE_TEXT_THRESHOLD,
    AA_NORMAL_TEXT_THRESHOLD,
    ContrastPairResult,
    check_theme_contrast,
    contrast_ratio,
    relative_luminance,
)

_WHITE = "#ffffff"
_BLACK = "#000000"

# --- Reference ratios ---


def test_black_on_white_is_the_maximum_21_to_1_ratio() -> None:
    assert contrast_ratio(_BLACK, _WHITE) == pytest.approx(21.0, abs=1e-9)


def test_white_on_white_is_the_minimum_1_to_1_ratio() -> None:
    assert contrast_ratio(_WHITE, _WHITE) == pytest.approx(1.0, abs=1e-9)


def test_black_on_black_is_the_minimum_1_to_1_ratio() -> None:
    assert contrast_ratio(_BLACK, _BLACK) == pytest.approx(1.0, abs=1e-9)


def test_ratio_is_symmetric_regardless_of_argument_order() -> None:
    assert contrast_ratio("#777777", _WHITE) == contrast_ratio(_WHITE, "#777777")


def test_relative_luminance_of_white_is_one() -> None:
    assert relative_luminance(_WHITE) == pytest.approx(1.0, abs=1e-9)


def test_relative_luminance_of_black_is_zero() -> None:
    assert relative_luminance(_BLACK) == pytest.approx(0.0, abs=1e-9)


def test_relative_luminance_rejects_malformed_hex() -> None:
    with pytest.raises(ValueError):
        relative_luminance("not-a-color")


def test_relative_luminance_rejects_short_hex_form() -> None:
    with pytest.raises(ValueError):
        relative_luminance("#fff")


# --- 4.5:1 normal-text AA threshold boundary ---
# #767676 on white ~= 4.542:1 (passes), #777777 on white ~= 4.478:1 (fails)
# — adjacent grays straddling the 4.5:1 line, verified against this
# module's own math (not an external calculator), so this pins the
# threshold comparison operator (>=) as well as the formula itself.


def test_just_above_normal_text_threshold_passes() -> None:
    ratio = contrast_ratio("#767676", _WHITE)
    assert ratio > AA_NORMAL_TEXT_THRESHOLD
    assert ratio >= AA_NORMAL_TEXT_THRESHOLD


def test_just_below_normal_text_threshold_fails() -> None:
    ratio = contrast_ratio("#777777", _WHITE)
    assert ratio < AA_NORMAL_TEXT_THRESHOLD


# --- 3:1 large-text/UI AA threshold boundary ---
# #949494 on white ~= 3.033:1 (passes), #959595 on white ~= 2.995:1 (fails)


def test_just_above_large_text_threshold_passes() -> None:
    ratio = contrast_ratio("#949494", _WHITE)
    assert ratio > AA_LARGE_TEXT_THRESHOLD


def test_just_below_large_text_threshold_fails() -> None:
    ratio = contrast_ratio("#959595", _WHITE)
    assert ratio < AA_LARGE_TEXT_THRESHOLD


# --- check_theme_contrast: report shape and pass/fail wiring ---
#
# Post-launch fix: narrowed from an earlier 9-pair scheme (each color vs.
# fixed white/black, plus primary-vs-accent) down to the 2 pairings this
# app actually renders text on top of anywhere (primary-on-secondary body
# text; secondary-on-accent button-label text) — see
# check_theme_contrast's own docstring for the full rationale (the old
# 7 extra pairs weren't real rendering constraints, and worse, a color
# mathematically cannot pass contrast against both white AND black at
# once, so "every pair passes" was unreachable for almost any real theme).


def test_check_theme_contrast_returns_two_pairs() -> None:
    report = check_theme_contrast(primary_color=_BLACK, secondary_color=_WHITE, accent_color="#c9a227")
    assert len(report.pairs) == 2
    assert {p.label for p in report.pairs} == {"primary vs secondary", "secondary vs accent"}
    assert all(isinstance(p, ContrastPairResult) for p in report.pairs)


def test_check_theme_contrast_foreground_background_roles_match_real_rendering() -> None:
    """primary-vs-secondary: primary is the foreground (real body text
    color), secondary the background. secondary-vs-accent: secondary is
    the foreground (the button LABEL color), accent the background (the
    button's own fill) -- see app.web.public_context.build_public_theme_css's
    .pub-button--primary rule, the only place accent is ever painted as a
    background with text on it."""
    report = check_theme_contrast(primary_color="#111111", secondary_color="#eeeeee", accent_color="#c9a227")
    by_label = {p.label: p for p in report.pairs}
    assert by_label["primary vs secondary"].foreground == "#111111"
    assert by_label["primary vs secondary"].background == "#eeeeee"
    assert by_label["secondary vs accent"].foreground == "#eeeeee"
    assert by_label["secondary vs accent"].background == "#c9a227"


def test_a_real_theme_can_now_pass_every_pair_simultaneously() -> None:
    """The whole point of narrowing the pair set: unlike the old 9-pair
    scheme (mathematically guaranteed to have at least one failure for
    almost any theme), a real theme with reasonable contrast choices
    should be able to clear BOTH remaining pairs at once."""
    report = check_theme_contrast(primary_color="#1a1a1a", secondary_color="#ffffff", accent_color="#0b3d66")
    assert report.all_pass_normal_text is True


def test_check_theme_contrast_flags_a_failing_pair() -> None:
    report = check_theme_contrast(primary_color="#1a1a1a", secondary_color="#ffffff", accent_color="#f5f5f5")
    by_label = {p.label: p for p in report.pairs}
    assert by_label["primary vs secondary"].passes_normal_text is True
    assert by_label["secondary vs accent"].passes_normal_text is False
    assert report.all_pass_normal_text is False


def test_ratio_on_pair_result_is_rounded_to_two_decimal_places() -> None:
    report = check_theme_contrast(primary_color="#767676", secondary_color=_WHITE, accent_color=_BLACK)
    pair = next(p for p in report.pairs if p.label == "primary vs secondary")
    assert pair.ratio == round(contrast_ratio("#767676", _WHITE), 2)

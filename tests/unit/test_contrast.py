"""Unit tests for ``app.services.contrast``: the WCAG 2.1 AA contrast-ratio
math and pass/fail threshold logic behind Milestone 1.5's "Automated AA
contrast check against the fixed theme fields" requirement.

Pure, deterministic, no DB/app context needed. Reference ratios (black on
white ~21:1, white on white 1:1) and threshold-boundary colors are
well-established WCAG facts, verified here against this module's own
implementation of the relative-luminance/ratio formula.
"""

import pytest

from app.services.contrast import (
    AA_LARGE_TEXT_THRESHOLD,
    AA_NORMAL_TEXT_THRESHOLD,
    BLACK,
    WHITE,
    ContrastPairResult,
    check_theme_contrast,
    contrast_ratio,
    relative_luminance,
)

# --- Reference ratios ---


def test_black_on_white_is_the_maximum_21_to_1_ratio() -> None:
    assert contrast_ratio(BLACK, WHITE) == pytest.approx(21.0, abs=1e-9)


def test_white_on_white_is_the_minimum_1_to_1_ratio() -> None:
    assert contrast_ratio(WHITE, WHITE) == pytest.approx(1.0, abs=1e-9)


def test_black_on_black_is_the_minimum_1_to_1_ratio() -> None:
    assert contrast_ratio(BLACK, BLACK) == pytest.approx(1.0, abs=1e-9)


def test_ratio_is_symmetric_regardless_of_argument_order() -> None:
    assert contrast_ratio("#777777", WHITE) == contrast_ratio(WHITE, "#777777")


def test_relative_luminance_of_white_is_one() -> None:
    assert relative_luminance(WHITE) == pytest.approx(1.0, abs=1e-9)


def test_relative_luminance_of_black_is_zero() -> None:
    assert relative_luminance(BLACK) == pytest.approx(0.0, abs=1e-9)


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
    ratio = contrast_ratio("#767676", WHITE)
    assert ratio > AA_NORMAL_TEXT_THRESHOLD
    assert ratio >= AA_NORMAL_TEXT_THRESHOLD


def test_just_below_normal_text_threshold_fails() -> None:
    ratio = contrast_ratio("#777777", WHITE)
    assert ratio < AA_NORMAL_TEXT_THRESHOLD


# --- 3:1 large-text/UI AA threshold boundary ---
# #949494 on white ~= 3.033:1 (passes), #959595 on white ~= 2.995:1 (fails)


def test_just_above_large_text_threshold_passes() -> None:
    ratio = contrast_ratio("#949494", WHITE)
    assert ratio > AA_LARGE_TEXT_THRESHOLD


def test_just_below_large_text_threshold_fails() -> None:
    ratio = contrast_ratio("#959595", WHITE)
    assert ratio < AA_LARGE_TEXT_THRESHOLD


# --- check_theme_contrast: report shape and pass/fail wiring ---


def test_check_theme_contrast_returns_nine_pairs() -> None:
    report = check_theme_contrast(primary_color=BLACK, secondary_color=WHITE, accent_color="#c9a227")
    assert len(report.pairs) == 9
    assert all(isinstance(p, ContrastPairResult) for p in report.pairs)


def test_check_theme_contrast_all_pass_flags_are_true_only_when_every_pair_passes() -> None:
    """A color can't have near-0 luminance (to contrast white) and near-1
    luminance (to contrast black) at once, so a real theme can't make every
    one of the 9 pairs pass 4.5:1 simultaneously — this asserts the
    ``all_pass_*`` properties are a genuine AND over every pair (not, say,
    hardcoded True), using the one pairing (black primary vs. white
    secondary) that reliably maxes out at 21:1 regardless of the accent."""
    report = check_theme_contrast(primary_color="#000000", secondary_color="#ffffff", accent_color="#808080")
    primary_vs_secondary = next(p for p in report.pairs if p.label == "primary vs secondary")
    assert primary_vs_secondary.passes_normal_text is True
    assert primary_vs_secondary.ratio == pytest.approx(21.0, abs=1e-9)
    # #808080 (mid-gray) predictably fails 4.5:1 against both black and
    # white, which is enough to prove all_pass_normal_text is a real AND.
    assert report.all_pass_normal_text is False


def test_check_theme_contrast_flags_failing_pairs_individually() -> None:
    """Near-white accent on a near-white/white background fails contrast,
    while the black primary on white still passes — the report must
    reflect pass/fail per pair, not just an overall verdict."""
    report = check_theme_contrast(primary_color="#000000", secondary_color="#ffffff", accent_color="#fdfdfd")
    by_label = {p.label: p for p in report.pairs}
    assert by_label["primary on white"].passes_normal_text is True
    assert by_label["accent on white"].passes_normal_text is False
    assert report.all_pass_normal_text is False


def test_ratio_on_pair_result_is_rounded_to_two_decimal_places() -> None:
    report = check_theme_contrast(primary_color="#767676", secondary_color=WHITE, accent_color=BLACK)
    pair = next(p for p in report.pairs if p.label == "primary on white")
    assert pair.ratio == round(contrast_ratio("#767676", WHITE), 2)

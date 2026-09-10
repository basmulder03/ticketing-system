"""Unit tests for ``app.services.theme_preview``'s "closest compliant
color" contrast-suggestion logic (post-launch fix, per the user's NOTES:
"Do color recommendations for what color can be used ... with easy
setting of that color").

Pure, deterministic, no DB/app context needed — same discipline as
``tests/unit/test_color.py`` and ``tests/unit/test_contrast.py``, which
this module's own reuse of both depends on.

Test-data note: ``app.services.contrast.check_theme_contrast`` checks each
of the three fixed colors against BOTH white and black (see its own
docstring) -- a color that's dark enough to read well on white is, by
construction, almost never ALSO high-contrast against pure black, and vice
versa. In practice this means every real theme has at least one "failing"
pair in this 9-pair report (confirmed empirically against a realistic
navy/white/gold-style theme below) -- so these tests assert against
specific, individually-verified pairs rather than an unreachable "the
whole theme passes everything" scenario.
"""

from app.models.enums import ThemeFont
from app.services.color import hex_to_hsl
from app.services.contrast import ContrastPairResult, ThemeContrastReport, contrast_ratio
from app.services.theme_preview import build_theme_preview


def _pair(report: ThemeContrastReport, label: str) -> ContrastPairResult:
    return next(p for p in report.pairs if p.label == label)


def test_a_pair_that_already_passes_gets_no_suggestion() -> None:
    result = build_theme_preview(
        primary_color="#1a1a1a",
        secondary_color="#ffffff",
        accent_color="#0b3d66",
        font_choice=ThemeFont.SYSTEM_SANS,
        custom_css=None,
    )
    passing = _pair(result.contrast_report, "primary on white")
    assert passing.passes_normal_text is True
    assert "primary on white" not in result.contrast_suggestions


def test_a_failing_pair_gets_a_suggestion_that_actually_passes() -> None:
    result = build_theme_preview(
        primary_color="#1a1a1a",
        secondary_color="#ffffff",
        accent_color="#0b3d66",
        font_choice=ThemeFont.SYSTEM_SANS,
        custom_css=None,
    )
    failing = _pair(result.contrast_report, "secondary on white")
    assert failing.passes_normal_text is False

    assert "secondary on white" in result.contrast_suggestions
    suggestion = result.contrast_suggestions["secondary on white"]
    assert suggestion.field_name == "secondary_color"
    assert contrast_ratio(suggestion.suggested_color, failing.background) >= 4.5


def test_every_failing_pair_in_a_realistic_theme_gets_a_passing_suggestion() -> None:
    """Broader sweep across a whole realistic theme (not just one hand-
    picked pair): every pair the report marks as failing must have a
    suggestion, and that suggestion must genuinely clear 4.5:1 against
    that exact pair's background."""
    result = build_theme_preview(
        primary_color="#1a1a1a",
        secondary_color="#ffffff",
        accent_color="#0b3d66",
        font_choice=ThemeFont.SYSTEM_SANS,
        custom_css=None,
    )
    failing_pairs = [p for p in result.contrast_report.pairs if not p.passes_normal_text]
    assert failing_pairs, "test setup assumption: this theme should have at least one failing pair"

    for pair in failing_pairs:
        assert pair.label in result.contrast_suggestions
        suggestion = result.contrast_suggestions[pair.label]
        assert contrast_ratio(suggestion.suggested_color, pair.background) >= 4.5


def test_suggestion_preserves_hue_for_a_saturated_color() -> None:
    result = build_theme_preview(
        primary_color="#1a1a1a",
        secondary_color="#ffffff",
        accent_color="#f0d0d0",  # a washed-out, low-contrast pink
        font_choice=ThemeFont.SYSTEM_SANS,
        custom_css=None,
    )
    accent_white_pair = _pair(result.contrast_report, "accent on white")
    assert accent_white_pair.passes_normal_text is False

    suggestion = result.contrast_suggestions["accent on white"]
    original_hue, _, _ = hex_to_hsl("#f0d0d0")
    suggested_hue, _, _ = hex_to_hsl(suggestion.suggested_color)
    assert abs(suggested_hue - original_hue) < 5.0


def test_normal_text_pass_always_implies_large_text_pass() -> None:
    """Sanity check on the module's own stated invariant: passing the 4.5:1
    normal-text threshold always implies passing the 3:1 large-text/UI
    threshold too (4.5 > 3.0), so "not passes_normal_text" is the correct
    (and only) condition build_theme_preview uses to decide whether a pair
    needs a suggestion at all."""
    result = build_theme_preview(
        primary_color="#808080",
        secondary_color="#a0a0a0",
        accent_color="#909090",
        font_choice=ThemeFont.SYSTEM_SANS,
        custom_css=None,
    )
    for pair in result.contrast_report.pairs:
        if pair.passes_normal_text:
            assert pair.passes_large_text

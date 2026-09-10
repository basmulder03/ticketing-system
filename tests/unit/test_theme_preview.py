"""Unit tests for ``app.services.theme_preview``'s "closest compliant
color" contrast-suggestion logic (post-launch fix, per the user's NOTES:
"Do color recommendations for what color can be used ... with easy
setting of that color").

Pure, deterministic, no DB/app context needed — same discipline as
``tests/unit/test_color.py`` and ``tests/unit/test_contrast.py``, which
this module's own reuse of both depends on.

Test-data note: since ``app.services.contrast.check_theme_contrast`` was
narrowed to the 2 pairs this app actually renders text on top of
(``primary vs secondary``, ``secondary vs accent`` — see that function's
own docstring for the full rationale), a real theme CAN pass both at once,
so these tests can construct a genuinely "everything already passes"
baseline theme, not just individually-verified failing pairs.

Note ``secondary_color`` still appears in BOTH pairs (background in the
first, foreground -- the button-label color -- in the second), so it is
never the field :func:`app.services.theme_preview._build_contrast_suggestions`
suggests adjusting (see that function's own docstring for why); the
convergence tests below exist specifically to prove that choice actually
delivers "apply every suggestion -> the whole report passes", not just
"each suggestion passes its own pair in isolation".
"""

import pytest

from app.models.enums import ThemeFont
from app.services.color import hex_to_hsl
from app.services.contrast import ContrastPairResult, ThemeContrastReport, contrast_ratio
from app.services.theme_preview import build_theme_preview


def _pair(report: ThemeContrastReport, label: str) -> ContrastPairResult:
    return next(p for p in report.pairs if p.label == label)


def test_no_suggestions_when_both_pairs_already_pass() -> None:
    result = build_theme_preview(
        primary_color="#1a1a1a",
        secondary_color="#ffffff",
        accent_color="#0b3d66",
        font_choice=ThemeFont.SYSTEM_SANS,
        custom_css=None,
    )
    assert result.contrast_report.all_pass_normal_text is True
    assert result.contrast_suggestions == {}


def test_a_failing_pair_gets_a_suggestion_that_actually_passes() -> None:
    # A near-white accent against a white secondary fails outright --
    # primary/secondary stay a safe, high-contrast pair.
    result = build_theme_preview(
        primary_color="#1a1a1a",
        secondary_color="#ffffff",
        accent_color="#f5f5f5",
        font_choice=ThemeFont.SYSTEM_SANS,
        custom_css=None,
    )
    failing = _pair(result.contrast_report, "secondary vs accent")
    assert failing.passes_normal_text is False
    passing = _pair(result.contrast_report, "primary vs secondary")
    assert passing.passes_normal_text is True
    assert "primary vs secondary" not in result.contrast_suggestions

    assert "secondary vs accent" in result.contrast_suggestions
    suggestion = result.contrast_suggestions["secondary vs accent"]
    assert suggestion.field_name == "accent_color"
    assert contrast_ratio(failing.foreground, suggestion.suggested_color) >= 4.5


def test_applying_every_suggestion_makes_the_whole_theme_pass() -> None:
    """The actual point of narrowing check_theme_contrast to 2 independent
    pairs (found necessary via the user's own testing: applying one
    suggestion used to be able to regress a DIFFERENT pair, since the old
    9-pair scheme could never be fully satisfied at once). Simulates
    "click every Use button": re-running build_theme_preview with the
    suggested colors substituted in must leave zero failing pairs and zero
    remaining suggestions."""
    result = build_theme_preview(
        primary_color="#1a1a1a",
        secondary_color="#eeeeee",
        accent_color="#f0f0f0",  # both pairs fail: primary/secondary ok, secondary/accent bad
        font_choice=ThemeFont.SYSTEM_SANS,
        custom_css=None,
    )
    assert result.contrast_suggestions, "test setup assumption: at least one pair should fail here"

    colors = {"primary_color": "#1a1a1a", "secondary_color": "#eeeeee", "accent_color": "#f0f0f0"}
    for suggestion in result.contrast_suggestions.values():
        colors[suggestion.field_name] = suggestion.suggested_color

    fixed = build_theme_preview(
        primary_color=colors["primary_color"],
        secondary_color=colors["secondary_color"],
        accent_color=colors["accent_color"],
        font_choice=ThemeFont.SYSTEM_SANS,
        custom_css=None,
    )
    assert fixed.contrast_report.all_pass_normal_text is True
    assert fixed.contrast_suggestions == {}


@pytest.mark.parametrize(
    ("primary", "secondary", "accent"),
    [
        ("#1a1a1a", "#eeeeee", "#f0f0f0"),  # accent fails against secondary
        ("#eeeeee", "#111111", "#0a0a0a"),  # inverted roles; both pairs fail
        ("#c9a227", "#ffffff", "#fdf6e3"),  # gold primary barely fails; near-white accent fails badly
        ("#5c1a1a", "#1a1a1a", "#2a1a1a"),  # everything dark and close together; both fail
        ("#ffffff", "#ffffff", "#ffffff"),  # degenerate: all three identical
    ],
)
def test_applying_every_suggestion_converges_across_varied_starting_themes(
    primary: str, secondary: str, accent: str
) -> None:
    """Broader sweep of test_applying_every_suggestion_makes_the_whole_theme_pass
    above, across starting themes chosen to fail in different ways
    (one pair, both pairs, and a fully-degenerate all-identical case) --
    convergence in one hand-picked case isn't enough to trust the "never
    touch secondary_color" design holds in general."""
    result = build_theme_preview(
        primary_color=primary,
        secondary_color=secondary,
        accent_color=accent,
        font_choice=ThemeFont.SYSTEM_SANS,
        custom_css=None,
    )

    colors = {"primary_color": primary, "secondary_color": secondary, "accent_color": accent}
    for suggestion in result.contrast_suggestions.values():
        colors[suggestion.field_name] = suggestion.suggested_color

    fixed = build_theme_preview(
        primary_color=colors["primary_color"],
        secondary_color=colors["secondary_color"],
        accent_color=colors["accent_color"],
        font_choice=ThemeFont.SYSTEM_SANS,
        custom_css=None,
    )
    assert fixed.contrast_report.all_pass_normal_text is True
    assert fixed.contrast_suggestions == {}
    # secondary_color itself must never have been touched -- the whole
    # point of the design.
    assert colors["secondary_color"] == secondary


def test_suggestion_preserves_hue_for_a_saturated_color() -> None:
    result = build_theme_preview(
        primary_color="#1a1a1a",
        secondary_color="#ffffff",
        accent_color="#f0d0d0",  # a washed-out, low-contrast pink
        font_choice=ThemeFont.SYSTEM_SANS,
        custom_css=None,
    )
    failing = _pair(result.contrast_report, "secondary vs accent")
    assert failing.passes_normal_text is False

    suggestion = result.contrast_suggestions["secondary vs accent"]
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

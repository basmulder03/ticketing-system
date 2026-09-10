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
    """Run the AA contrast check against the two color pairings this app
    actually renders text on top of, everywhere a Theme is applied (the
    public landing page, ticket/invoice PDFs, outgoing emails, and the
    backoffice's own live-preview sample):

    1. **primary on secondary** — the Theme's default body text/background
       (``.event-content { color: primary; background: secondary; }`` —
       see ``app.web.public_context.build_public_theme_css`` and the
       identical pairing in ``app.services.email_render``,
       ``app.services.ticket_pdf``, ``app.services.invoice_pdf``).
    2. **secondary on accent** — the primary call-to-action button's label
       on its own background (``.pub-button--primary { background: accent;
       color: secondary; }``) — the only place ``accent_color`` is ever
       painted as a background with real text on top of it anywhere in
       this codebase (elsewhere it's decorative only: a border, a bar —
       see ``app.services.ticket_pdf``/``invoice_pdf``'s accent-colored
       rules, which carry no text).

    Revision history/rationale (post-launch fix, found via the user's own
    manual testing): earlier versions of this function additionally
    checked each of the three colors against fixed pure white/black, plus
    "primary vs accent" — a broader defensive net "documented judgement
    call" per this function's own prior docstring, since the brief itself
    doesn't prescribe which combinations matter. In practice this caused
    two real problems: (a) none of those 7 extra pairs correspond to any
    real rendered pixel in this app (confirmed by auditing every template/
    CSS/PDF/email module that touches these three fields — accent is never
    on-screen as text, primary/secondary are never painted against white/
    black except via each other), and (b) far worse, a color literally
    CANNOT have strong contrast against both pure white and pure black at
    once (near-0 luminance clears one, near-1 luminance clears the other,
    never both) — so "every pair passes" was mathematically unreachable
    for almost any real theme, and the backoffice's old "closest compliant
    color" suggestion feature would visibly fight itself: fixing one of
    those synthetic pairs could regress another, with no way to ever reach
    a fully-green table. Narrowing to just the 2 pairs above fixes both
    problems at once: they're the only ones a viewer can actually see, and
    — since ``secondary`` is the only field shared between them, always as
    a BACKGROUND in both — a suggested fix for ``primary`` (against
    ``secondary``) can never affect whether ``accent`` (against that same
    ``secondary``) still passes, and vice versa. The two pairs are
    independent, so applying every suggestion this module's callers offer
    is now guaranteed to reach full compliance, not just reduce the
    failure count.

    This intentionally does NOT and cannot check ``custom_css`` — see this
    module's docstring.
    """
    pairs = [
        _check_pair("primary vs secondary", primary_color, secondary_color),
        _check_pair("secondary vs accent", secondary_color, accent_color),
    ]
    return ThemeContrastReport(pairs=pairs)

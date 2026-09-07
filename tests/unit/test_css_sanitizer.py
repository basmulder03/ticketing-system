"""Unit tests for ``app.core.css_sanitizer.sanitize_custom_css``.

This is the security-critical pure function behind Milestone 1.5's
"advanced custom CSS override" — per PROJECT_BRIEF.md, it must strip
``@import``, external ``url()`` references, ``position: fixed`` abuse, and
anything targeting elements outside the event's content container. Pure
function, no DB/app context needed, so every case here asserts the *exact*
sanitized output (not just "doesn't crash" or "doesn't contain X") —
expected values were derived by running the real function against each
input, not guessed, since tinycss2's re-serialization has its own exact
whitespace/formatting conventions.
"""

from app.core.css_sanitizer import sanitize_custom_css

# --- Bare/unscoped selectors: dropped entirely ---


def test_bare_body_selector_is_dropped() -> None:
    assert sanitize_custom_css("body { color: red; }") == ""


def test_bare_html_selector_is_dropped() -> None:
    assert sanitize_custom_css("html { color: red; }") == ""


def test_bare_universal_selector_is_dropped() -> None:
    assert sanitize_custom_css("* { color: red; }") == ""


def test_bare_root_selector_is_dropped() -> None:
    assert sanitize_custom_css(":root { color: red; }") == ""


def test_scoped_selector_survives() -> None:
    assert sanitize_custom_css(".event-content { color: red; }") == ".event-content { color: red; }"


def test_scoped_descendant_selector_survives() -> None:
    css = ".event-content h1 { color: red; }"
    assert sanitize_custom_css(css) == css


# --- Selector lists: one unscoped branch voids the WHOLE rule ---


def test_selector_list_with_one_unscoped_branch_drops_the_whole_rule() -> None:
    """``.event-content, body { ... }`` must be dropped entirely — not
    silently rewritten to keep only the scoped branch — since partial
    rewriting is exactly the kind of leniency that invites bypasses."""
    assert sanitize_custom_css(".event-content, body { color: red; }") == ""


def test_selector_list_with_all_branches_scoped_survives() -> None:
    css = ".event-content h1, .event-content h2 { color: red; }"
    assert sanitize_custom_css(css) == css


def test_not_pseudo_class_commas_are_not_treated_as_top_level_separators() -> None:
    """Commas nested inside a function/block (e.g. ``:not(a, b)``) must NOT
    be mistaken for top-level selector-list separators — the whole
    ``:not(a, b)`` selector is a single scoped branch, so it survives."""
    css = ".event-content:not(a, b) { color: red; }"
    assert sanitize_custom_css(css) == css


# --- @import, including unicode-escape bypass attempts ---


def test_at_import_is_dropped() -> None:
    assert sanitize_custom_css("@import url('evil.css');") == ""


def test_at_import_unicode_escaped_keyword_is_still_dropped() -> None:
    """``@\\69mport`` is ``@import`` with the ``i`` unicode-escaped — a real
    CSS tokenizer resolves this the same way a browser would, so this must
    be caught exactly like the unescaped form (this is the whole reason the
    sanitizer is built on tinycss2 rather than a regex over raw text)."""
    assert sanitize_custom_css("@\\69mport url('evil.css');") == ""


def test_at_import_alongside_a_valid_rule_only_drops_the_import() -> None:
    css = "@import url('evil.css'); .event-content { color: red; }"
    assert sanitize_custom_css(css) == ".event-content { color: red; }"


# --- url() references ---


def test_external_absolute_scheme_url_is_stripped_but_rule_survives_empty() -> None:
    css = ".event-content { background: url(http://evil.com/x.png); }"
    assert sanitize_custom_css(css) == ".event-content {  }"


def test_protocol_relative_url_is_stripped() -> None:
    css = ".event-content { background: url(//evil.com/x.png); }"
    assert sanitize_custom_css(css) == ".event-content {  }"


def test_data_url_is_stripped() -> None:
    """``data:`` carries an explicit scheme, same as ``http:``/``https:`` —
    it is not a same-origin-relative path, so it is treated as external and
    stripped, same as any other scheme."""
    css = ".event-content { background: url(data:image/png;base64,AAAA); }"
    assert sanitize_custom_css(css) == ".event-content {  }"


def test_relative_url_survives() -> None:
    css = ".event-content { background: url(/img/x.png); }"
    assert sanitize_custom_css(css) == css


def test_relative_url_without_leading_slash_survives() -> None:
    css = ".event-content { background: url(img/x.png); }"
    assert sanitize_custom_css(css) == css


# --- position: fixed / sticky stripped, absolute allowed ---


def test_position_fixed_is_stripped_but_sibling_declaration_survives() -> None:
    css = ".event-content { position: fixed; color: red; }"
    assert sanitize_custom_css(css) == ".event-content { color: red; }"


def test_position_sticky_is_stripped() -> None:
    css = ".event-content { position: sticky; color: blue; }"
    assert sanitize_custom_css(css) == ".event-content { color: blue; }"


def test_position_absolute_is_not_stripped() -> None:
    css = ".event-content { position: absolute; color: green; }"
    assert sanitize_custom_css(css) == ".event-content { position: absolute;color: green; }"


# --- expression() ---


def test_expression_declaration_is_stripped() -> None:
    css = ".event-content { width: expression(alert(1)); color: red; }"
    assert sanitize_custom_css(css) == ".event-content { color: red; }"


# --- @media / @supports: recursively sanitized containers ---


def test_media_at_rule_is_recursively_sanitized() -> None:
    """Inside a ``@media`` block, a scoped rule survives and an unscoped
    (``body``) rule is still dropped — the same rules apply recursively."""
    css = "@media (min-width: 500px) { .event-content { color: red; } body { color: blue; } }"
    assert sanitize_custom_css(css) == "@media (min-width: 500px) { .event-content { color: red; } }"


def test_supports_at_rule_is_recursively_sanitized() -> None:
    css = "@supports (display: grid) { .event-content { display: grid; } }"
    assert sanitize_custom_css(css) == css


def test_media_at_rule_with_only_unscoped_content_is_dropped_entirely() -> None:
    assert sanitize_custom_css("@media (min-width: 500px) { body { color: blue; } }") == ""


# --- Disallowed at-rules: dropped entirely, no partial keep ---


def test_font_face_is_dropped() -> None:
    assert sanitize_custom_css("@font-face { font-family: X; src: url(x.woff); }") == ""


def test_keyframes_is_dropped() -> None:
    assert sanitize_custom_css("@keyframes spin { from { transform: rotate(0); } }") == ""


def test_page_is_dropped() -> None:
    assert sanitize_custom_css("@page { margin: 1in; }") == ""


def test_charset_is_dropped() -> None:
    assert sanitize_custom_css('@charset "UTF-8";') == ""


# --- Depth guard against pathological nesting ---


def _nest_media(inner_css: str, depth: int) -> str:
    css = inner_css
    for _ in range(depth):
        css = f"@media (min-width: 1px) {{ {css} }}"
    return css


def test_media_nesting_at_the_depth_guard_boundary_is_kept() -> None:
    css = _nest_media(".event-content { color: red; }", depth=8)
    assert "color: red" in sanitize_custom_css(css)


def test_media_nesting_past_the_depth_guard_is_dropped() -> None:
    css = _nest_media(".event-content { color: red; }", depth=9)
    assert sanitize_custom_css(css) == ""


# --- Empty/whitespace-only input ---


def test_empty_string_returns_empty_string() -> None:
    assert sanitize_custom_css("") == ""


def test_whitespace_only_input_returns_empty_string() -> None:
    assert sanitize_custom_css("   \n\t  ") == ""


# --- Sibling-combinator scoping bypass (security-reviewer finding, Milestone 1.5) ---
#
# A branch prefixed with ".event-content" but containing a later sibling
# combinator (~ or +) can still target an element OUTSIDE the container's
# subtree — the earlier scoping check only looked at the first two tokens
# and left everything after "unrestricted". Only descendant (whitespace)
# and child (>) combinators keep every matched element inside the subtree.


def test_general_sibling_combinator_after_scope_prefix_is_dropped() -> None:
    assert sanitize_custom_css(".event-content ~ footer { color: red; }") == ""


def test_adjacent_sibling_combinator_after_scope_prefix_is_dropped() -> None:
    assert sanitize_custom_css('.event-content + footer::after { content: "INJECTED"; }') == ""


def test_general_sibling_with_universal_selector_is_dropped() -> None:
    assert sanitize_custom_css(".event-content ~ * { background: yellow; }") == ""


def test_child_combinator_after_scope_prefix_still_survives() -> None:
    assert sanitize_custom_css(".event-content > h1 { color: red; }") == ".event-content > h1 { color: red; }"


# --- </style> breakout via declaration string values (security-reviewer
# finding, Milestone 1.5) ---
#
# Nothing else in this module inspects the CONTENT of a string-valued
# declaration (e.g. `content: "..."` is not a url()/expression()/position
# value), so a literal "</style>" inside a string sailed through every
# other check unchanged. Both today's live-preview iframe and the eventual
# public event page embed this function's output directly inside a literal
# <style> block, where the browser's HTML tokenizer ends that block at the
# first "</style" byte sequence regardless of CSS validity — so the
# sanitizer itself must guarantee its output can never contain that
# sequence, not rely on every embedding call site to escape correctly.


def test_style_tag_breakout_via_content_string_is_neutralized() -> None:
    payload = '.event-content::after { content: "</style><script>alert(1)</script>"; }'
    result = sanitize_custom_css(payload)
    assert "</style" not in result.lower()
    assert "<script" not in result.lower()


def test_bare_less_than_in_content_string_is_escaped() -> None:
    result = sanitize_custom_css('.event-content::after { content: "<b>hi"; }')
    assert "<" not in result
    assert "\\3C " in result

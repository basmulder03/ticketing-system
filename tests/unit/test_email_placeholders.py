"""Unit tests for ``app.services.email_placeholders.render_placeholders`` —
the security-critical adversarial matrix (Milestone 4), mirroring
``tests/unit/test_css_sanitizer.py``'s rigor for the same category of
function ("agent-writable content that later renders as HTML").

Every case here asserts against the exact rendered string, not just
"doesn't crash" — see this module's docstring in
``app/services/email_placeholders.py`` for the exact security properties
being proven.
"""

from app.services.email_placeholders import PLACEHOLDER_PATTERN, render_placeholders

# --- Basic substitution ------------------------------------------------------


def test_simple_known_placeholder_is_substituted() -> None:
    result = render_placeholders("Hi {{buyer_name}}!", {"buyer_name": "Jamie"}, escape_html=False)
    assert result == "Hi Jamie!"


def test_multiple_placeholders_all_substituted() -> None:
    result = render_placeholders(
        "{{buyer_name}} owes {{order_total}}",
        {"buyer_name": "Jamie", "order_total": "€42.50"},
        escape_html=False,
    )
    assert result == "Jamie owes €42.50"


def test_repeated_placeholder_substituted_every_occurrence() -> None:
    result = render_placeholders("{{buyer_name}}, hi {{buyer_name}}", {"buyer_name": "Jamie"}, escape_html=False)
    assert result == "Jamie, hi Jamie"


# --- Script/markup injection: escape_html=True vs. False --------------------


def test_script_tag_buyer_name_is_html_escaped_when_escape_html_true() -> None:
    payload = "<script>alert(1)</script>"
    result = render_placeholders("Hi {{buyer_name}}", {"buyer_name": payload}, escape_html=True)
    assert "<script>" not in result
    assert result == "Hi &lt;script&gt;alert(1)&lt;/script&gt;"


def test_script_tag_buyer_name_passes_through_unescaped_when_escape_html_false() -> None:
    """``escape_html=False`` is for plain-text-only destinations (subject,
    plain-text body) — no HTML escaping, but control characters are still
    stripped (covered separately below)."""
    payload = "<script>alert(1)</script>"
    result = render_placeholders("Hi {{buyer_name}}", {"buyer_name": payload}, escape_html=False)
    assert result == "Hi <script>alert(1)</script>"


def test_table_breakout_payload_is_html_escaped_when_true() -> None:
    payload = "</td><td>injected</td>"
    result = render_placeholders("{{buyer_name}}", {"buyer_name": payload}, escape_html=True)
    assert "<" not in result and ">" not in result
    assert result == "&lt;/td&gt;&lt;td&gt;injected&lt;/td&gt;"


# --- CRLF / header-injection guard: stripped in BOTH modes ------------------


def test_crlf_in_value_is_stripped_when_escape_html_true() -> None:
    payload = "Jamie\r\nBcc: evil@example.com"
    result = render_placeholders("{{buyer_name}}", {"buyer_name": payload}, escape_html=True)
    assert "\r" not in result
    assert "\n" not in result
    assert result == "JamieBcc: evil@example.com"


def test_crlf_in_value_is_stripped_when_escape_html_false() -> None:
    payload = "Jamie\r\nBcc: evil@example.com"
    result = render_placeholders("{{buyer_name}}", {"buyer_name": payload}, escape_html=False)
    assert "\r" not in result
    assert "\n" not in result
    assert result == "JamieBcc: evil@example.com"


def test_bare_lf_only_is_also_stripped() -> None:
    result = render_placeholders("{{buyer_name}}", {"buyer_name": "a\nb"}, escape_html=False)
    assert result == "ab"


def test_bare_cr_only_is_also_stripped() -> None:
    result = render_placeholders("{{buyer_name}}", {"buyer_name": "a\rb"}, escape_html=False)
    assert result == "ab"


def test_other_control_characters_are_stripped_but_tab_is_kept() -> None:
    payload = "a\tb\x00c\x07d"
    result = render_placeholders("{{buyer_name}}", {"buyer_name": payload}, escape_html=False)
    assert result == "a\tbcd"


# --- CRLF in the literal template text itself (not via a placeholder) -------


def test_crlf_in_literal_subject_text_is_stripped_when_escape_html_false() -> None:
    """An agent/admin can type a raw CRLF directly into stored
    ``EmailTemplate.subject`` text (not through any ``{{placeholder}}``) —
    this must be stripped too, not just substituted values, since the
    subject is destined for a plain email header."""
    text = "Your tickets\r\nBcc: evil@example.com"
    result = render_placeholders(text, {}, escape_html=False)
    assert "\r" not in result
    assert "\n" not in result
    assert result == "Your ticketsBcc: evil@example.com"


def test_bare_lf_in_literal_subject_text_is_stripped() -> None:
    text = "Your tickets\nBcc: evil@example.com"
    result = render_placeholders(text, {}, escape_html=False)
    assert "\n" not in result
    assert result == "Your ticketsBcc: evil@example.com"


def test_crlf_in_literal_subject_text_stripped_alongside_real_placeholder() -> None:
    """The literal-text stripping and placeholder substitution compose
    correctly: the CRLF around the placeholder is stripped and the known
    key is still substituted."""
    text = "Hi {{buyer_name}}\r\nBcc: evil@example.com"
    result = render_placeholders(text, {"buyer_name": "Jamie"}, escape_html=False)
    assert result == "Hi JamieBcc: evil@example.com"


def test_crlf_in_literal_body_text_is_left_alone_when_escape_html_true() -> None:
    """A literal newline in stored HTML body source is harmless whitespace
    there (never a header value), so this module intentionally does NOT
    strip it when ``escape_html=True`` — only the header/plain-text
    (``escape_html=False``) case needs literal-text sanitization."""
    text = "<p>Hi</p>\r\n<p>{{buyer_name}}</p>"
    result = render_placeholders(text, {"buyer_name": "Jamie"}, escape_html=True)
    assert "\r\n" in result
    assert result == "<p>Hi</p>\r\n<p>Jamie</p>"


# --- Malformed placeholder syntax: left as literal text, never raises -------


def test_stray_open_braces_left_as_literal_text() -> None:
    assert render_placeholders("a {{ b", {}, escape_html=False) == "a {{ b"


def test_stray_close_braces_left_as_literal_text() -> None:
    assert render_placeholders("a }} b", {"buyer_name": "x"}, escape_html=False) == "a }} b"


def test_spaced_placeholder_does_not_match_and_is_left_literal() -> None:
    text = "Hi {{ buyer_name }}"
    result = render_placeholders(text, {"buyer_name": "Jamie"}, escape_html=False)
    assert result == text


def test_nested_braces_with_unknown_inner_key_left_entirely_literal() -> None:
    """``{{nested{{brace}}}}`` — verified live rather than assumed: the
    regex actually finds an inner match ``{{brace}}`` (the innermost valid
    ``{{key}}`` shape, starting at the second ``{{``), leaving the outer
    ``{{nested`` / ``}}`` as surrounding literal text. Since ``brace`` is
    not a known key here, that inner match is also left untouched
    (:func:`render_placeholders`'s "unknown key" rule), so the WHOLE
    malformed string round-trips unchanged — never raises, never partially
    mangles the text."""
    text = "{{nested{{brace}}}}"
    result = render_placeholders(text, {}, escape_html=False)
    assert result == text


def test_nested_braces_with_known_inner_key_only_substitutes_the_inner_match() -> None:
    """Same input, but ``brace`` IS a known key: only the inner ``{{brace}}``
    (the actual regex match) is substituted — proving this is plain
    non-recursive regex substitution, not a real parser that understands
    "nesting" as a concept at all."""
    result = render_placeholders("{{nested{{brace}}}}", {"brace": "X"}, escape_html=False)
    assert result == "{{nestedX}}"


def test_unknown_key_placeholder_left_untouched_verbatim() -> None:
    text = "Hi {{unknown_key}}!"
    result = render_placeholders(text, {"buyer_name": "Jamie"}, escape_html=False)
    assert result == text


def test_unknown_key_alongside_known_key_only_substitutes_the_known_one() -> None:
    text = "{{buyer_name}} / {{unknown_key}}"
    result = render_placeholders(text, {"buyer_name": "Jamie"}, escape_html=False)
    assert result == "Jamie / {{unknown_key}}"


# --- No template-language / expression evaluation ---------------------------


def test_jinja_style_if_block_is_never_evaluated_passes_through_literally() -> None:
    text = "{% if x %}Hi{% endif %}"
    result = render_placeholders(text, {"x": "1"}, escape_html=False)
    assert result == text


def test_dotted_attribute_access_does_not_match_and_is_left_literal() -> None:
    """``{{ x.__class__ }}`` contains a space and dots — neither matches
    ``PLACEHOLDER_PATTERN``'s ``[a-zA-Z0-9_]+``-only character class, so it
    is never even recognized as a placeholder, let alone evaluated."""
    text = "{{ x.__class__ }}"
    assert PLACEHOLDER_PATTERN.search(text) is None
    result = render_placeholders(text, {"x": "irrelevant"}, escape_html=False)
    assert result == text


def test_arithmetic_expression_is_never_evaluated() -> None:
    """Proves no expression evaluation happens at all: a naive template
    engine might compute ``7*7 == 49``; this module must never produce
    "49" anywhere in the output for this input."""
    text = "{{ 7*7 }}"
    result = render_placeholders(text, {}, escape_html=False)
    assert "49" not in result
    assert result == text


def test_arithmetic_expression_never_evaluated_even_with_escape_html_true() -> None:
    text = "{{ 7*7 }}"
    result = render_placeholders(text, {}, escape_html=True)
    assert "49" not in result
    assert result == text


# --- Never raises ------------------------------------------------------------


def test_empty_text_returns_empty_string() -> None:
    assert render_placeholders("", {"buyer_name": "Jamie"}, escape_html=False) == ""


def test_empty_values_mapping_does_not_raise() -> None:
    text = "Hi {{buyer_name}}"
    assert render_placeholders(text, {}, escape_html=False) == text


def test_non_string_looking_key_names_in_template_are_still_just_literal_when_unknown() -> None:
    text = "{{123abc}}"
    result = render_placeholders(text, {}, escape_html=False)
    assert result == text

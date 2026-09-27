"""Default-deny sanitizer for a Theme's custom CSS.

The CSS is tokenized with ``tinycss2`` first, so escape tricks (e.g.
``@\\69mport``) are resolved before any check runs — a regex over raw text
would miss them. Both the save path and the preview endpoint use this same
function, so the preview can't bypass it. What survives:

- Rules whose *every* selector branch starts with ``.event-content`` and
  uses only descendant/child combinators. One bad branch drops the whole rule.
- ``@media``/``@supports`` (recursively sanitized). Every other at-rule
  (``@import``, ``@font-face``, ``@keyframes``, ...) is dropped.
- Declarations, except ``position: fixed|sticky``, ``expression()``, and any
  ``url()`` with a scheme or ``//`` (blocks tracking/exfiltration). A bad
  declaration is dropped whole, never rewritten.

Anything else (stray tokens, parse errors) is dropped.
"""

import re

import tinycss2
import tinycss2.ast as css_ast

EVENT_CONTENT_CLASS = "event-content"
"""Every surviving selector must be scoped under this class; public templates
must wrap themed content in an element carrying it."""

_MAX_NESTING_DEPTH = 8
"""Deeper ``@media``/``@supports`` nesting is dropped, bounding recursion."""

_ALLOWED_CONTAINER_AT_KEYWORDS = frozenset({"media", "supports"})

_DISALLOWED_POSITION_VALUES = frozenset({"fixed", "sticky"})

_EXTERNAL_URL_PATTERN = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.\-]*:|//)")
"""Any URL with a scheme (``http:``, ``data:``, ``javascript:``...) or ``//``.
Relative paths and ``#fragment`` don't match and are allowed."""


_DISALLOWED_COMBINATORS = frozenset({"~", "+"})
"""Sibling combinators: ``.event-content ~ footer`` starts with the right
class but targets an element *outside* the container. Only descendant and
child combinators keep matches inside it."""


def _selector_branch_is_scoped(branch_tokens: list[object]) -> bool:
    """True if the branch starts with ``.event-content`` and contains no
    sibling combinator."""
    significant = [t for t in branch_tokens if not isinstance(t, css_ast.WhitespaceToken)]
    if len(significant) < 2:
        return False
    first, second = significant[0], significant[1]
    is_dot = isinstance(first, css_ast.LiteralToken) and first.value == "."
    is_class_name = isinstance(second, css_ast.IdentToken) and second.value == EVENT_CONTENT_CLASS
    if not (is_dot and is_class_name):
        return False
    return not any(_token_is_disallowed_combinator(t) for t in significant[2:])


def _token_is_disallowed_combinator(token: object) -> bool:
    """Checks both the literal and the CSS-escaped form (``\\7E `` tokenizes
    as an ``IdentToken``). The escaped form isn't a working combinator, but
    rejecting it too keeps the invariant explicit."""
    if isinstance(token, css_ast.LiteralToken):
        return token.value in _DISALLOWED_COMBINATORS
    if isinstance(token, css_ast.IdentToken):
        return token.value in _DISALLOWED_COMBINATORS
    return False


def _split_top_level_commas(tokens: list[object]) -> list[list[object]]:
    """Split on top-level commas. Commas inside ``:not(a, b)`` etc. are
    already grouped into one block token by tinycss2."""
    branches: list[list[object]] = [[]]
    for token in tokens:
        if isinstance(token, css_ast.LiteralToken) and token.value == ",":
            branches.append([])
        else:
            branches[-1].append(token)
    return branches


def _selector_is_allowed(prelude: list[object]) -> bool:
    """Allowed only if every comma-separated branch is scoped."""
    branches = _split_top_level_commas(prelude)
    if not branches:
        return False
    return all(_selector_branch_is_scoped(branch) for branch in branches)


def _value_has_disallowed_url_or_expression(value_tokens: list[object]) -> bool:
    """True if the value holds an external ``url()`` or ``expression()``,
    at any nesting depth (e.g. inside ``image-set(...)``)."""
    for token in value_tokens:
        if isinstance(token, css_ast.URLToken):
            if _EXTERNAL_URL_PATTERN.match(token.value):
                return True
        elif isinstance(token, css_ast.FunctionBlock):
            if token.lower_name == "expression":
                return True
            if token.lower_name == "url":
                url_value = "".join(
                    arg.value for arg in token.arguments if isinstance(arg, css_ast.StringToken)
                )
                if _EXTERNAL_URL_PATTERN.match(url_value):
                    return True
            if _value_has_disallowed_url_or_expression(token.arguments):
                return True
        elif isinstance(
            token, (css_ast.ParenthesesBlock, css_ast.SquareBracketsBlock, css_ast.CurlyBracketsBlock)
        ) and _value_has_disallowed_url_or_expression(token.content):
            return True
    return False


def _declaration_is_allowed(declaration: css_ast.Declaration) -> bool:
    """Reject ``position: fixed|sticky`` and external url/expression values."""
    if declaration.lower_name == "position":
        value_idents = {
            t.lower_value for t in declaration.value if isinstance(t, css_ast.IdentToken)
        }
        if value_idents & _DISALLOWED_POSITION_VALUES:
            return False
    return not _value_has_disallowed_url_or_expression(declaration.value)


def _sanitize_declarations(raw_content: list[object]) -> str:
    """Re-serialize a rule body with disallowed declarations removed."""
    parsed = tinycss2.parse_declaration_list(raw_content, skip_comments=True, skip_whitespace=True)
    kept = [
        d for d in parsed if isinstance(d, css_ast.Declaration) and _declaration_is_allowed(d)
    ]
    return str(tinycss2.serialize(kept))


def _sanitize_rule_list(rules: list[object], depth: int) -> str:
    """Rebuild safe CSS from ``rules``, recursing into allowed at-rules."""
    output_parts: list[str] = []
    for rule in rules:
        if isinstance(rule, css_ast.QualifiedRule):
            if not _selector_is_allowed(rule.prelude):
                continue
            selector_text = tinycss2.serialize(rule.prelude).strip()
            declarations_text = _sanitize_declarations(rule.content)
            output_parts.append(f"{selector_text} {{ {declarations_text} }}")
        elif isinstance(rule, css_ast.AtRule):
            if rule.lower_at_keyword not in _ALLOWED_CONTAINER_AT_KEYWORDS or depth >= _MAX_NESTING_DEPTH:
                continue
            if rule.content is None:
                continue
            prelude_text = tinycss2.serialize(rule.prelude).strip()
            inner_rules = tinycss2.parse_stylesheet(rule.content, skip_comments=True, skip_whitespace=True)
            inner_text = _sanitize_rule_list(inner_rules, depth + 1)
            if inner_text:
                output_parts.append(f"@{rule.lower_at_keyword} {prelude_text} {{ {inner_text} }}")
        # Anything else is dropped (default-deny).
    return "\n".join(output_parts)


def _escape_angle_brackets_for_style_embedding(css_text: str) -> str:
    """Escape every ``<`` as ``\\3C `` so output can never contain ``</style``.

    Declaration values like ``content: "..."`` accept arbitrary strings that
    no other check touches, and this output is embedded in a literal
    ``<style>`` block, where the HTML parser ends the block at the first
    ``</style`` regardless of CSS syntax. The CSS escape renders identically.
    Doing it here makes every embedding site safe by construction.
    """
    return css_text.replace("<", "\\3C ")


def sanitize_custom_css(raw_css: str) -> str:
    """Return sanitized CSS, safe to embed directly inside ``<style>`` (no
    further escaping needed). Pure and deterministic; blank input → ``""``."""
    if not raw_css.strip():
        return ""
    rules = tinycss2.parse_stylesheet(raw_css, skip_comments=True, skip_whitespace=True)
    sanitized = _sanitize_rule_list(rules, depth=0)
    return _escape_angle_brackets_for_style_embedding(sanitized)

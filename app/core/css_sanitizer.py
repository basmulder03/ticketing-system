"""Server-side sanitizer for a Theme's optional "advanced" custom CSS override.

Per PROJECT_BRIEF.md's Event & Theming section, custom CSS "must be
sanitized/sandboxed server-side before storage/render": strip ``@import``,
external ``url()`` references, ``position: fixed`` abuse, and anything
targeting elements outside the event's content container. This is the
single, pure implementation of that rule — both the real save path
(``app.api.routes.themes``) and the preview endpoint route through this
same function, so the preview pane can never be used to bypass sanitization.

Design: default-deny, not a blocklist bolted onto free-form text. A real
CSS parser (``tinycss2`` — already a transitive dependency via
``weasyprint``/``cssselect2``, now a direct one) tokenizes the input first,
so escape/encoding tricks (e.g. ``@\\69mport`` for ``@import``) are resolved
by the tokenizer before any of our checks run, the same way a browser's own
CSS parser would resolve them — a regex-based sanitizer operating on raw
text would miss this class of bypass entirely.

What survives sanitization:
- Qualified (selector) rules whose *every* comma-separated selector branch
  is scoped under :data:`EVENT_CONTENT_CLASS` (i.e. starts with
  ``.event-content`` as the outermost simple selector) — a rule with even
  one out-of-scope branch (e.g. ``.event-content, body``) is dropped
  entirely, not partially rewritten, since guessing which branch was
  "intended" is exactly the kind of leniency that invites bypasses.
- ``@media`` / ``@supports`` at-rules (the only two at-rules that are
  themselves just conditional containers for further selector rules,
  recursively sanitized the same way) — every other at-rule (``@import``,
  ``@font-face``, ``@keyframes``, ``@page``, ``@charset``, ``@namespace``,
  etc.) is dropped outright. ``@font-face`` and ``@keyframes`` are
  deliberately excluded even though the brief doesn't name them: neither
  has a selector that can be scoped to the content container, and
  animations/custom fonts aren't a stated requirement for this milestone —
  flagged in the handoff for `frontend-theming`/a future milestone if
  wanted.
- Declarations, EXCEPT: ``position: fixed`` / ``position: sticky`` (sticky
  is not named explicitly in the brief but is the same class of
  scroll-escaping overlay abuse as fixed positioning — judgement call,
  documented here), any declaration whose value contains an ``expression()``
  function (the legacy IE CSS-expression code-execution vector — dead in
  every modern browser, but explicitly worth blocking since it costs
  nothing and is a named adversarial-input class), and any declaration
  whose value contains a ``url()`` reference to anything other than a
  same-origin/relative path (no scheme, no protocol-relative ``//`` —
  this is what stops background-image-based exfiltration/tracking pixels).
  A declaration that fails any of these checks is dropped in its entirety
  (not rewritten), same reasoning as the selector-branch rule.

Anything that doesn't parse as a clean qualified rule or a
media/supports at-rule (stray tokens, parse errors, unknown at-rules) is
silently dropped rather than passed through — default-deny.
"""

import re

import tinycss2
import tinycss2.ast as css_ast

EVENT_CONTENT_CLASS = "event-content"
"""The single fixed container class every surviving selector must be scoped
under. Kept as a module constant (not a parameter) — the actual public
landing page template `frontend-theming` builds against this milestone's
output MUST wrap themed content in an element carrying exactly this class,
or every custom-CSS rule a user writes will be (correctly) stripped."""

_MAX_NESTING_DEPTH = 8
"""Guards against pathological/adversarial deeply-nested ``@media`` input
driving unbounded recursion. Real theme CSS never needs anything close to
this; anything nested deeper than this is dropped rather than recursed
into."""

_ALLOWED_CONTAINER_AT_KEYWORDS = frozenset({"media", "supports"})

_DISALLOWED_POSITION_VALUES = frozenset({"fixed", "sticky"})

_EXTERNAL_URL_PATTERN = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.\-]*:|//)")
"""Matches a URL that carries an explicit scheme (``http:``, ``data:``,
``javascript:``, ...) or is protocol-relative (``//host/...``) — i.e.
anything that is NOT same-origin-relative. A bare path (``/img/x.png``,
``img/x.png``, ``../x.png``, ``#fragment``) does not match and is allowed."""


def _selector_branch_is_scoped(branch_tokens: list[object]) -> bool:
    """Return True if a single (comma-split) selector branch is scoped under
    :data:`EVENT_CONTENT_CLASS`, i.e. its first significant tokens are the
    class selector ``.event-content`` (optionally followed by further
    simple selectors, combinators, or descendants — anything after the
    required prefix is unrestricted).
    """
    significant = [t for t in branch_tokens if not isinstance(t, css_ast.WhitespaceToken)]
    if len(significant) < 2:
        return False
    first, second = significant[0], significant[1]
    is_dot = isinstance(first, css_ast.LiteralToken) and first.value == "."
    is_class_name = isinstance(second, css_ast.IdentToken) and second.value == EVENT_CONTENT_CLASS
    return is_dot and is_class_name


def _split_top_level_commas(tokens: list[object]) -> list[list[object]]:
    """Split a token list on top-level comma literals.

    Commas nested inside a function/parenthesis/bracket block (e.g.
    ``:not(a, b)``) are NOT top-level — tinycss2 already groups those into a
    single block token, so a naive top-level split is safe here without any
    custom bracket-depth tracking.
    """
    branches: list[list[object]] = [[]]
    for token in tokens:
        if isinstance(token, css_ast.LiteralToken) and token.value == ",":
            branches.append([])
        else:
            branches[-1].append(token)
    return branches


def _selector_is_allowed(prelude: list[object]) -> bool:
    """A full selector list (rule prelude) is allowed only if EVERY
    comma-separated branch is individually scoped. One out-of-scope branch
    voids the whole rule (default-deny, no partial rewriting)."""
    branches = _split_top_level_commas(prelude)
    if not branches:
        return False
    return all(_selector_branch_is_scoped(branch) for branch in branches)


def _value_has_disallowed_url_or_expression(value_tokens: list[object]) -> bool:
    """Return True if any token in a declaration's value is a disallowed
    ``url()``/``URLToken`` reference (external scheme or protocol-relative)
    or an ``expression()`` function call. Recurses into nested function/
    block tokens so a smuggled ``url()`` inside e.g. ``image-set(...)``
    or a nested function is still caught."""
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
    """Apply the position/url/expression checks to a single declaration."""
    if declaration.lower_name == "position":
        value_idents = {
            t.lower_value for t in declaration.value if isinstance(t, css_ast.IdentToken)
        }
        if value_idents & _DISALLOWED_POSITION_VALUES:
            return False
    return not _value_has_disallowed_url_or_expression(declaration.value)


def _sanitize_declarations(raw_content: list[object]) -> str:
    """Parse a qualified rule's ``{ ... }`` body as a declaration list,
    drop any disallowed declaration, and re-serialize what remains."""
    parsed = tinycss2.parse_declaration_list(raw_content, skip_comments=True, skip_whitespace=True)
    kept = [
        d for d in parsed if isinstance(d, css_ast.Declaration) and _declaration_is_allowed(d)
    ]
    return str(tinycss2.serialize(kept))


def _sanitize_rule_list(rules: list[object], depth: int) -> str:
    """Sanitize a list of top-level (or ``@media``/``@supports``-nested)
    rules, returning the reconstructed, safe CSS text. Recurses for nested
    container at-rules up to :data:`_MAX_NESTING_DEPTH`."""
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
        # Anything else (bare declarations at the top level, parse errors,
        # comments) is silently dropped — default-deny.
    return "\n".join(output_parts)


def sanitize_custom_css(raw_css: str) -> str:
    """Sanitize ``raw_css`` per this module's rules and return safe CSS text.

    Pure function: no I/O, no DB access, deterministic on input alone —
    intentionally isolated so ``test-writer`` can hammer it with adversarial
    inputs and ``security-reviewer`` can audit it without any other app
    context. Both the real Theme save path and the preview endpoint call
    this exact function (never a "preview-only" relaxed variant).

    An empty or whitespace-only input returns ``""``.
    """
    if not raw_css.strip():
        return ""
    rules = tinycss2.parse_stylesheet(raw_css, skip_comments=True, skip_whitespace=True)
    return _sanitize_rule_list(rules, depth=0)

"""Placeholder substitution for agent/admin-editable ``EmailTemplate``
``subject``/``body`` content (Milestone 4).

**Security-critical module** — read this docstring before changing
anything here. ``EmailTemplate.body`` is agent-writable content (per
PROJECT_BRIEF.md's AI/Agent Access section, "email template content" is
explicitly in scope for agent API keys) AND every real render also
interpolates buyer-submitted data (``buyer_name``) into an HTML email sent
to a real recipient. That combination is exactly the "agent-writable
content that later renders as HTML" + "buyer-submitted free text that later
renders into a document" risk pattern this project has hit before.

Two independent properties this module guarantees, both load-bearing:

1. **No template-language features, ever.** Placeholder syntax is a fixed,
   trivial ``{{key}}`` token — resolved by straight text substitution
   (:func:`re.sub` with a literal-value replacement), never by handing the
   template string to a real template engine (no Jinja2
   ``Template(...).render()`` on this content). An agent (or a compromised
   agent key) can at most choose which of a fixed set of *known* values
   appears where in its own subject/body wording — it can never make the
   renderer evaluate an expression, loop, attribute-access, or call
   anything. This is why :func:`render_placeholders` is a regex substitution
   and will never grow ``{% %}``/expression support.
2. **Every substituted VALUE is sanitized before insertion**, regardless of
   who supplied the value:
   - Control characters (notably ``\\r``/``\\n``) are always stripped from
     every value, whether it lands in an HTML body or a plain header
     (``subject``) — a buyer name containing a CRLF sequence must never be
     able to inject extra email headers (CRLF/header injection) when
     substituted into ``EmailMessage["Subject"]``.
   - When ``escape_html=True`` (used for the HTML body — see
     ``app.services.email_render``), every value additionally goes through
     ``html.escape`` — a buyer named ``<script>alert(1)</script>`` or
     ``</td><td>`` renders as inert literal text in the email, never as
     markup that could break the surrounding table-based layout or inject
     content into someone else's inbox render.

Unknown/malformed placeholder syntax in the template text is handled
without ever raising: text that doesn't match the ``{{key}}`` pattern at
all (a stray ``{{`` or ``}}``) is simply left as literal text by
:func:`re.sub` (nothing to substitute); a syntactically well-formed
``{{some_unknown_key}}`` for a key not present in the supplied values dict
is also left untouched verbatim, rather than being treated as an error or
silently deleted — an agent-authored template with a typo'd placeholder
degrades to visibly showing the placeholder text, not a 500 or a stack
trace leaking into a real payment-confirmation email.

Pure function, no I/O — intentionally isolated the same way
``app.core.css_sanitizer.sanitize_custom_css`` is, so ``test-writer`` can
hammer it with adversarial ``buyer_name``/template inputs directly and
``security-reviewer`` can audit it without any other app context.
"""

import html
import re
from collections.abc import Mapping

__all__ = ["PLACEHOLDER_PATTERN", "render_placeholders"]

PLACEHOLDER_PATTERN = re.compile(r"\{\{([a-zA-Z0-9_]+)\}\}")
"""A placeholder is exactly ``{{name}}`` where ``name`` is one or more
ASCII letters/digits/underscores — no whitespace, no dotted/attribute
access, no filters or expressions. This is the ONLY syntax this module
understands; anything else (unbalanced braces, non-identifier characters
inside the braces, nested braces) simply never matches and passes through
as literal template text."""


def _sanitize_value(value: str) -> str:
    """Strip control characters (including ``\\r``/``\\n``) from a
    placeholder value, keeping plain tabs.

    Applied to EVERY substituted value regardless of destination (HTML body
    or plain-text ``subject`` header) — this is what stops a buyer-supplied
    value (e.g. ``buyer_name``) from smuggling a newline into an email
    header (CRLF/header injection) if it's ever used in ``subject``, and
    keeps HTML body substitutions from containing invisible control bytes.
    """
    return "".join(ch for ch in value if ch == "\t" or ch >= " ")


def render_placeholders(text: str, values: Mapping[str, str], *, escape_html: bool) -> str:
    """Substitute every well-formed ``{{key}}`` placeholder in ``text`` with
    ``str(values[key])`` (sanitized — see :func:`_sanitize_value`, and
    HTML-escaped too when ``escape_html`` is True), leaving anything else
    (malformed syntax, or a placeholder naming a key not present in
    ``values``) untouched as literal text.

    ``escape_html`` MUST be ``True`` for any substitution destined for an
    HTML document (the email body) and should be ``False`` only for a
    plain-text destination that itself gets no further HTML rendering (the
    email ``subject`` header, or the plain-text email alternative) — see
    this module's docstring for why both paths still sanitize control
    characters unconditionally.

    Never raises: this function has no failure mode other than "leave the
    ambiguous/unknown bit of text alone."
    """

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            return match.group(0)
        sanitized = _sanitize_value(str(values[key]))
        return html.escape(sanitized) if escape_html else sanitized

    return PLACEHOLDER_PATTERN.sub(_replace, text)

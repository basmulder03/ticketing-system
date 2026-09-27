"""Security-critical: ``{{placeholder}}`` substitution for email templates.

Template text is agent/admin-writable and values include buyer input, so:

1. **No template language, ever.** A plain regex swaps ``{{key}}`` for a
   known value. Never hand this text to Jinja — an agent must not be able
   to evaluate anything.
2. **Every value is sanitized.** Control characters (incl. CR/LF) are
   stripped from every value, so a buyer name can't inject email headers;
   HTML-bound values are also ``html.escape``d. For plain-text destinations
   (the subject) the template text itself is stripped too, rather than
   relying on ``EmailMessage`` rejecting CR/LF downstream.

Malformed or unknown placeholders are left as literal text — never an
error. Pure function, no I/O.
"""

import html
import re
from collections.abc import Mapping

__all__ = ["PLACEHOLDER_PATTERN", "render_placeholders"]

PLACEHOLDER_PATTERN = re.compile(r"\{\{([a-zA-Z0-9_]+)\}\}")
"""Exactly ``{{name}}`` with ``[A-Za-z0-9_]+`` — no spaces, dots, filters or
expressions. Anything else never matches.
"""


def _sanitize_value(value: str) -> str:
    """Strip control characters (incl. CR/LF), keeping tabs."""
    return "".join(ch for ch in value if ch == "\t" or ch >= " ")


def render_placeholders(text: str, values: Mapping[str, str], *, escape_html: bool) -> str:
    """Replace each known ``{{key}}`` with its sanitized value; leave the rest.

    Use ``escape_html=True`` for HTML (the body) and ``False`` only for plain
    text (the subject, the text alternative). With ``False`` the template text
    is stripped of control characters too. Never raises.
    """
    if not escape_html:
        text = _sanitize_value(text)

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            return match.group(0)
        sanitized = _sanitize_value(str(values[key]))
        return html.escape(sanitized) if escape_html else sanitized

    return PLACEHOLDER_PATTERN.sub(_replace, text)

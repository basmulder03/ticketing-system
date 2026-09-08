"""Shared "redirect with a flash message" helper for backoffice web routes.

Extracted out of ``app.web.routes.themes`` (which defined its own private
copy of this exact function first, in Milestone 1.5) so the two Milestone 4
route modules that need the same pattern (``app.web.routes.email_templates``,
``app.web.routes.orders``) don't each grow a third near-identical copy —
DRY per PROJECT_BRIEF.md's coding standards. ``app.web.routes.themes`` keeps
its own copy untouched (out of this milestone's scope to refactor).
"""

from urllib.parse import quote

from fastapi.responses import RedirectResponse


def redirect_with_flash(path: str, message: str, kind: str = "success") -> RedirectResponse:
    """Redirect to ``path`` with a ``flash=...&flash_kind=...`` query string
    appended that ``backoffice/base.html`` reads to render a one-off flash
    banner on the next page load.

    ``path`` may already carry its own query string (e.g. the EmailTemplate
    editor's ``?language=en``) — appended with ``&`` in that case rather
    than a second ``?``, which would otherwise get swallowed into the
    existing query string's last value instead of being parsed as its own
    key (verified against a real bug caught during this milestone's manual
    end-to-end testing: the flash banner silently never appeared after a
    save/reset redirect on that page).
    """
    separator = "&" if "?" in path else "?"
    return RedirectResponse(
        url=f"{path}{separator}flash={quote(message)}&flash_kind={kind}", status_code=303
    )

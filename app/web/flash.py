"""Redirect-with-flash-message helper for backoffice routes."""

from urllib.parse import quote

from fastapi.responses import RedirectResponse


def redirect_with_flash(path: str, message: str, kind: str = "success") -> RedirectResponse:
    """Redirect to ``path`` with ``flash``/``flash_kind`` query params, which
    ``backoffice/base.html`` shows as a banner. Appends with ``&`` if ``path``
    already has a query string (a second ``?`` would swallow the flash).
    """
    separator = "&" if "?" in path else "?"
    return RedirectResponse(
        url=f"{path}{separator}flash={quote(message)}&flash_kind={kind}", status_code=303
    )

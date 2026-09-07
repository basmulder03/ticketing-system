"""Server-rendered backoffice HTML pages (Jinja2 + HTMX), as opposed to the
JSON API under ``app.api.routes``.

Every route in ``app.web.routes.*`` is a thin page/form controller: it
authenticates the human admin (``app.web.deps.require_web_admin``), talks to
the existing JSON API in-process (``app.web.api_client``) so validation,
sanitization, contrast-checking, and audit logging stay defined in exactly
one place, and renders a Jinja2 template. No business logic lives here.
"""

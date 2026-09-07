"""Jinja2 template environment for server-rendered PUBLIC pages (Milestone 2).

Deliberately separate from ``app.core.templating`` (the backoffice
environment) — see that module's own docstring, which already flags this
split: the backoffice has one fixed look, but every public page is themed
per-Event (dynamic colors/font/background/custom CSS pulled from the
active ``Theme``), so sharing one Jinja environment/template directory
between the two would blur that boundary and risk a backoffice template
accidentally reusing public-only globals (or vice versa).

``translate`` (see ``app.i18n``) is registered as a template global here so
every public template can call ``translate('some.key', locale)`` directly
without importing it per-view — the single approved path to user-facing
text in these templates (see PROJECT_BRIEF.md's Internationalization
section: "no hardcoded strings in templates").
"""

from pathlib import Path
from urllib.parse import urlencode

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.i18n import translate

PUBLIC_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
"""The shared ``app/templates`` root (same directory ``app.core.templating``
points at) — public templates live under its ``public/`` subfolder (e.g.
``public/landing.html``), mirroring how the backoffice environment's own
templates live under ``backoffice/``. Two separate ``Jinja2Templates``
instances (different globals, see below) can safely share one root
directory; they're isolated by which subfolder each one's templates
actually reference."""

public_templates = Jinja2Templates(directory=str(PUBLIC_TEMPLATES_DIR))
public_templates.env.globals["translate"] = translate


def with_query_param(request: Request, key: str, value: str) -> str:
    """Build a relative URL (current path + query string) with ``key``
    overridden to ``value`` — used by the language-switcher links in
    ``public/base.html`` so switching language preserves other query
    params already on the page (e.g. a deep-linked ``?ticket_type=``)
    instead of dropping them.
    """
    params = dict(request.query_params)
    params[key] = value
    return f"?{urlencode(params)}"


public_templates.env.globals["with_query_param"] = with_query_param

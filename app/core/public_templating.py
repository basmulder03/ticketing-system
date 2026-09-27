"""Jinja2 environment for public (per-event themed) pages, separate from the
backoffice one so neither leaks globals into the other.

All user-facing text goes through the ``translate`` global — no hardcoded
strings in public templates.
"""

from pathlib import Path
from urllib.parse import urlencode

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.i18n import translate
from app.i18n.formatting import format_currency, format_date, format_time

PUBLIC_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
"""Shared with the backoffice environment; public templates live in ``public/``."""

public_templates = Jinja2Templates(directory=str(PUBLIC_TEMPLATES_DIR))
public_templates.env.globals["translate"] = translate

# e.g. {{ show.date | format_date(locale) }}, {{ tt.price | format_currency(locale) }}
public_templates.env.filters["format_date"] = format_date
public_templates.env.filters["format_time"] = format_time
public_templates.env.filters["format_currency"] = format_currency


def with_query_param(request: Request, key: str, value: str) -> str:
    """Current query string with ``key`` set to ``value``, keeping other
    params (used by the language switcher)."""
    params = dict(request.query_params)
    params[key] = value
    return f"?{urlencode(params)}"


public_templates.env.globals["with_query_param"] = with_query_param

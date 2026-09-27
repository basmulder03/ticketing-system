"""Hands a just-created order's details from the checkout POST to the
confirmation page via a short-lived httponly cookie.

A cookie rather than a public "fetch order by id" route, because order
details must not be reachable by link: the page only works in the buyer's
own browser, so a shared URL shows nothing. The downside is that it doesn't
survive a cleared cookie jar or another device; signed per-buyer order links
would fix that if ever needed.
"""

import json
from typing import Any

from fastapi import Request, Response

from app.core.config import get_settings

ORDER_CONFIRMATION_COOKIE = "beacon_order_confirmation"
_MAX_AGE_SECONDS = 30 * 60
_COOKIE_PATH = "/order-confirmation"


def stash_order_confirmation(response: Response, order_payload: dict[str, Any]) -> None:
    """Set the order cookie on ``response``, scoped to the confirmation path
    only, for 30 minutes.
    """
    settings = get_settings()
    response.set_cookie(
        key=ORDER_CONFIRMATION_COOKIE,
        value=json.dumps(order_payload),
        max_age=_MAX_AGE_SECONDS,
        path=_COOKIE_PATH,
        httponly=True,
        samesite="lax",
        secure=settings.app_env != "development",
    )


def read_order_confirmation(request: Request, order_id: str) -> dict[str, Any] | None:
    """The stashed order if it matches ``order_id``, else ``None``."""
    raw = request.cookies.get(ORDER_CONFIRMATION_COOKIE)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("id") != order_id:
        return None
    return payload

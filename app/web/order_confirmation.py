"""Ephemeral, buyer-browser-only hand-off of a just-created Order's details
from the checkout POST to the confirmation GET page.

Design note (why a cookie, not a public "fetch order by id" API route):
PROJECT_BRIEF.md's Sharing section is explicit that "Order confirmation/
ticket links are never made shareable... those stay behind the buyer's
unique signed access" — and no such signed-access mechanism exists yet
(that's Milestone 4's QR/ticket-link work). Rather than add a public,
unauthenticated "GET order by id" endpoint now (which would itself be a
shareable/guessable link, undermining that exact requirement, and is
backend scope this agent shouldn't add unasked), the checkout web route
stashes the just-created order's own response in a short-lived, httponly
cookie scoped to the confirmation path, and the confirmation GET reads it
back. This has the useful side effect of making the confirmation page
*structurally* unshareable (per the brief) — a link to it does nothing in
anyone else's browser, since they won't have the cookie — rather than
relying on "please don't share this" alone.

Flagged for `backend-builder`/a future milestone: once Milestone 4 adds
signed per-buyer order/ticket access tokens, this cookie shim should be
replaced by that real mechanism (e.g. so an order confirmation page
survives a cleared cookie jar or a different device).
"""

import json
from typing import Any

from fastapi import Request, Response

from app.core.config import get_settings

ORDER_CONFIRMATION_COOKIE = "beacon_order_confirmation"
_MAX_AGE_SECONDS = 30 * 60
_COOKIE_PATH = "/order-confirmation"


def stash_order_confirmation(response: Response, order_payload: dict[str, Any]) -> None:
    """Attach the just-created order's details to ``response`` as a
    short-lived cookie, scoped to :data:`_COOKIE_PATH` only (never sent on
    any other request), for the immediate post-checkout redirect to read
    back. 30 minutes is enough for "did my checkout go through" review,
    deliberately not long-lived — this is a hand-off, not durable ticket
    access."""
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
    """Return the stashed order payload if present and it matches
    ``order_id`` (defense against a stale/mismatched cookie from a
    different, earlier order), else ``None``."""
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

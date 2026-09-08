"""Minimal backoffice Orders view (Milestone 4): a list of an Event's
Orders (id/status/buyer name/email/total/created date) with an inline
"Resend confirmation email" action per PROJECT_BRIEF.md's Ticket Generation
& Delivery section ("Backoffice action to resend a ticket").

Deliberately minimal per this milestone's scope — no mark-as-paid action,
no filtering/sorting UI, no stats: that is Milestone 6/8 territory. This
exists only because nothing else in the backoffice can reach an Order at
all yet, and the resend action needs somewhere to find one.

This page's data comes from ``GET /api/v1/events/{event_id}/orders``
(admin-only, mirrors every other "list under an event" route's shape —
see ``app.api.routes.orders.list_orders``), returning a JSON array of
order objects shaped like ``app.schemas.order.OrderOut`` (id, event_id,
status, payment_method, buyer_name, buyer_email, buyer_address, language,
total, tickets, created_at). ``_fetch_orders_context`` still degrades to
an empty list with a visible "could not load orders" banner on any
non-200/404 response, rather than crashing the page — defensive against a
future regression in that route, not because the route is missing.
Verified end-to-end against a real order (checkout -> paid -> orders list
renders it -> resend sends a second email).
"""

from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from app.api.deps import Principal
from app.core.templating import templates
from app.web.api_client import internal_api_client
from app.web.csrf import attach_csrf_cookie, read_or_generate_csrf_token, verify_csrf
from app.web.deps import require_web_admin
from app.web.flash import redirect_with_flash

router = APIRouter(tags=["backoffice-orders"])


def _error_detail(response: Any, fallback: str) -> str:
    try:
        detail = response.json().get("detail", fallback)
    except Exception:  # noqa: BLE001 - response body may not be JSON at all
        return fallback
    return detail if isinstance(detail, str) else fallback


async def _fetch_orders_context(request: Request, event_id: str) -> dict[str, Any]:
    async with internal_api_client(request) as client:
        event_response = await client.get(f"/api/v1/events/{event_id}")
        if event_response.status_code == 404:
            raise HTTPException(status_code=404, detail="Event not found.")
        event = event_response.json()

        orders_response = await client.get(f"/api/v1/events/{event_id}/orders")

    if orders_response.status_code == 200:
        return {"event": event, "orders": orders_response.json(), "orders_error": None}

    # Defensive fallback (event genuinely not found got its own 404 above,
    # via event_response) — an unexpected error here degrades to an empty
    # list + banner rather than crashing the whole page.
    orders_error = _error_detail(orders_response, "Could not load orders for this event.")
    return {"event": event, "orders": [], "orders_error": orders_error}


@router.get("/events/{event_id}/orders", response_model=None)
async def orders_list(
    request: Request, event_id: str, principal: Principal = Depends(require_web_admin)
) -> Response:
    ctx = await _fetch_orders_context(request, event_id)
    token = read_or_generate_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "backoffice/orders_list.html",
        {
            "principal": principal,
            "event": ctx["event"],
            "orders": ctx["orders"],
            "orders_error": ctx["orders_error"],
            "csrf_token": token,
            "flash": request.query_params.get("flash"),
            "flash_kind": request.query_params.get("flash_kind", "success"),
        },
    )
    attach_csrf_cookie(response, token)
    return response


@router.post("/events/{event_id}/orders/{order_id}/resend")
async def resend_order_confirmation(
    request: Request,
    event_id: str,
    order_id: str,
    principal: Principal = Depends(require_web_admin),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    """Proxy to the existing admin-only
    ``POST /api/v1/orders/{order_id}/resend-confirmation-email`` route (see
    ``app.api.routes.orders``) — that route already exists and works today,
    independent of the list-endpoint gap noted in this module's docstring."""
    verify_csrf(request, csrf_token)
    redirect_path = f"/events/{event_id}/orders"

    async with internal_api_client(request) as client:
        resp = await client.post(f"/api/v1/orders/{order_id}/resend-confirmation-email")

    if resp.status_code == 404:
        return redirect_with_flash(redirect_path, "Order not found.", kind="error")
    if resp.status_code == 409:
        return redirect_with_flash(
            redirect_path, _error_detail(resp, "Only paid orders can be resent."), kind="error"
        )
    if resp.status_code >= 400:
        return redirect_with_flash(
            redirect_path,
            _error_detail(resp, "Could not resend the confirmation email."),
            kind="error",
        )

    sent = resp.json().get("sent", False)
    if sent:
        return redirect_with_flash(redirect_path, "Confirmation email resent.", kind="success")
    return redirect_with_flash(
        redirect_path,
        "Resend attempted but the email failed to send — check SMTP configuration and the audit log.",
        kind="error",
    )

"""Backoffice orders page for one event, with inline actions: resend
confirmation, mark as paid, and erase buyer PII. The mark-as-paid form is
also embedded in the scanner's "unpaid" screen, hence its ``return_to``.
"""

import re
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from app.api.deps import Principal
from app.core.templating import templates
from app.web.api_client import api_error_detail, internal_api_client
from app.web.csrf import attach_csrf_cookie, read_or_generate_csrf_token, verify_csrf
from app.web.deps import require_web_admin
from app.web.flash import redirect_with_flash

router = APIRouter(tags=["backoffice-orders"])

_CONTROL_OR_BACKSLASH = re.compile(r"[\\\t\r\n]")
"""Same as ``app.web.routes.auth._CONTROL_OR_BACKSLASH``."""


def _safe_return_to(candidate: str, *, default: str) -> str:
    """Open-redirect guard like ``auth._safe_next``, with a caller-supplied fallback."""
    if not candidate:
        return default
    if _CONTROL_OR_BACKSLASH.search(candidate):
        return default
    if not candidate.startswith("/") or candidate.startswith("//"):
        return default
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc:
        return default
    return candidate


async def _fetch_orders_context(request: Request, event_id: str) -> dict[str, Any]:
    async with internal_api_client(request) as client:
        event_response = await client.get(f"/api/v1/events/{event_id}")
        if event_response.status_code == 404:
            raise HTTPException(status_code=404, detail="Event not found.")
        event = event_response.json()

        orders_response = await client.get(f"/api/v1/events/{event_id}/orders")

    if orders_response.status_code == 200:
        return {"event": event, "orders": orders_response.json(), "orders_error": None}

    # Unexpected error: show an empty list with a banner instead of crashing.
    orders_error = api_error_detail(orders_response, "Could not load orders for this event.")
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
    """Resend the confirmation email."""
    verify_csrf(request, csrf_token)
    redirect_path = f"/events/{event_id}/orders"

    async with internal_api_client(request) as client:
        resp = await client.post(f"/api/v1/orders/{order_id}/resend-confirmation-email")

    if resp.status_code == 404:
        return redirect_with_flash(redirect_path, "Order not found.", kind="error")
    if resp.status_code == 409:
        return redirect_with_flash(
            redirect_path, api_error_detail(resp, "Only paid orders can be resent."), kind="error"
        )
    if resp.status_code >= 400:
        return redirect_with_flash(
            redirect_path,
            api_error_detail(resp, "Could not resend the confirmation email."),
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


@router.post("/events/{event_id}/orders/{order_id}/mark-paid")
async def mark_order_paid_web(
    request: Request,
    event_id: str,
    order_id: str,
    principal: Principal = Depends(require_web_admin),
    csrf_token: str = Form(...),
    method_label: str = Form(...),
    reason: str = Form(""),
    return_to: str = Form(""),
) -> RedirectResponse:
    """Mark an order paid (any non-paid status; the API is the enforcement point).
    ``return_to`` lets the scanner page send the admin back there to rescan;
    without it, back to the orders list.
    """
    verify_csrf(request, csrf_token)
    default_redirect = f"/events/{event_id}/orders"
    redirect_path = _safe_return_to(return_to, default=default_redirect)

    label = method_label.strip()
    if not label:
        return redirect_with_flash(redirect_path, "A payment method/label is required.", kind="error")

    body: dict[str, Any] = {"method_label": label}
    reason_clean = reason.strip()
    if reason_clean:
        body["reason"] = reason_clean

    async with internal_api_client(request) as client:
        resp = await client.post(f"/api/v1/orders/{order_id}/mark-paid", json=body)

    if resp.status_code == 404:
        return redirect_with_flash(redirect_path, "Order not found.", kind="error")
    if resp.status_code >= 400:
        return redirect_with_flash(
            redirect_path,
            api_error_detail(resp, "Could not mark this order as paid."),
            kind="error",
        )

    already_paid = resp.json().get("already_paid", False)
    if already_paid:
        return redirect_with_flash(redirect_path, "Order was already marked as paid.", kind="success")
    return redirect_with_flash(redirect_path, "Order marked as paid.", kind="success")


@router.post("/events/{event_id}/orders/{order_id}/erase-pii")
async def erase_order_pii_web(
    request: Request,
    event_id: str,
    order_id: str,
    principal: Principal = Depends(require_web_admin),
    csrf_token: str = Form(...),
    confirm: bool = Form(False),
) -> RedirectResponse:
    """Erase buyer PII.

    The plain button sends no ``confirm`` (fine for uninvoiced orders). Paid
    orders — which always have an invoice — also get a second button with
    ``confirm=true`` whose dialog shows the retention warning first. If the API
    still 409s, its message is flashed with a pointer to that button.
    """
    verify_csrf(request, csrf_token)
    redirect_path = f"/events/{event_id}/orders"

    async with internal_api_client(request) as client:
        resp = await client.post(f"/api/v1/orders/{order_id}/erase-pii", json={"confirm": confirm})

    if resp.status_code == 404:
        return redirect_with_flash(redirect_path, "Order not found.", kind="error")
    if resp.status_code == 409:
        warning = api_error_detail(resp, "This order has an issued invoice and requires confirmation to erase.")
        return redirect_with_flash(
            redirect_path,
            f"{warning} Use the \"Erase anyway (has an invoice)\" button below to proceed.",
            kind="error",
        )
    if resp.status_code >= 400:
        return redirect_with_flash(
            redirect_path,
            api_error_detail(resp, "Could not erase this order's buyer details."),
            kind="error",
        )

    had_invoice = resp.json().get("had_invoice", False)
    if had_invoice:
        return redirect_with_flash(
            redirect_path,
            "Buyer details erased. This order's invoice was retained, per your invoice-retention obligations.",
            kind="success",
        )
    return redirect_with_flash(redirect_path, "Buyer details erased.", kind="success")

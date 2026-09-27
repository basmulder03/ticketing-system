"""Backoffice "Manage shows" page: shows, their ticket types, duplication, and
issuing tickets manually — all proxied to the JSON API.

Edit forms carry hidden ``orig_<field>`` twins, and only changed fields are
sent. That avoids no-op audit entries and avoids overwriting a field another
admin changed since the page loaded.
"""

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from app.api.deps import Principal
from app.core.templating import templates
from app.web.api_client import api_error_detail, internal_api_client
from app.web.csrf import attach_csrf_cookie, read_or_generate_csrf_token, verify_csrf
from app.web.deps import require_web_admin
from app.web.flash import redirect_with_flash

router = APIRouter(tags=["backoffice-shows"])

# Select options; the API enforces the real enum.
STATUS_CHOICES = [
    ("draft", "Draft"),
    ("published", "Published"),
]


def _parse_int_or_none(raw: str) -> int | None:
    """Parse a number field; ``None`` if not numeric (browser validation isn't trusted)."""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def _fetch_shows_context(request: Request, event_id: str) -> dict[str, Any]:
    """The event and its shows, each with its ticket types (live ``remaining``).
    One call per show — fine in-process for a small admin page.
    """
    async with internal_api_client(request) as client:
        event_response = await client.get(f"/api/v1/events/{event_id}")
        if event_response.status_code == 404:
            raise HTTPException(status_code=404, detail="Event not found.")
        event = event_response.json()

        shows_response = await client.get(f"/api/v1/events/{event_id}/shows")
        if shows_response.status_code != 200:
            return {
                "event": event,
                "shows": [],
                "shows_error": api_error_detail(shows_response, "Could not load shows for this event."),
            }
        shows = shows_response.json()

        for show in shows:
            ticket_types_response = await client.get(f"/api/v1/shows/{show['id']}/ticket-types")
            show["ticket_types"] = ticket_types_response.json() if ticket_types_response.status_code == 200 else []

    return {"event": event, "shows": shows, "shows_error": None}


@router.get("/events/{event_id}/shows", response_model=None)
async def shows_manage(
    request: Request, event_id: str, principal: Principal = Depends(require_web_admin)
) -> Response:
    """Render the page. ``?open=<show_id>`` keeps that show's panel open after a save."""
    ctx = await _fetch_shows_context(request, event_id)
    token = read_or_generate_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "backoffice/shows_manage.html",
        {
            "principal": principal,
            "event": ctx["event"],
            "shows": ctx["shows"],
            "shows_error": ctx["shows_error"],
            "status_choices": STATUS_CHOICES,
            "open_show_id": request.query_params.get("open"),
            "csrf_token": token,
            "flash": request.query_params.get("flash"),
            "flash_kind": request.query_params.get("flash_kind", "success"),
        },
    )
    attach_csrf_cookie(response, token)
    return response


@router.post("/events/{event_id}/shows")
async def create_show_web(
    request: Request,
    event_id: str,
    principal: Principal = Depends(require_web_admin),
    csrf_token: str = Form(...),
    date: str = Form(...),
    doors_time: str = Form(...),
    start_time: str = Form(...),
    venue_name: str = Form(...),
    venue_address: str = Form(...),
    capacity: str = Form(...),
    status: str = Form("draft"),
) -> RedirectResponse:
    """Create a show; API validation errors are flashed. Note that nothing checks
    doors time is before start time.
    """
    verify_csrf(request, csrf_token)
    redirect_path = f"/events/{event_id}/shows"

    capacity_int = _parse_int_or_none(capacity)
    if capacity_int is None:
        return redirect_with_flash(redirect_path, "Capacity must be a whole number.", kind="error")

    body = {
        "date": date,
        "doors_time": doors_time,
        "start_time": start_time,
        "venue_name": venue_name,
        "venue_address": venue_address,
        "capacity": capacity_int,
        "status": status,
    }
    async with internal_api_client(request) as client:
        resp = await client.post(f"/api/v1/events/{event_id}/shows", json=body)

    if resp.status_code >= 400:
        return redirect_with_flash(redirect_path, api_error_detail(resp, "Could not add this show."), kind="error")

    new_show_id = resp.json().get("id")
    return redirect_with_flash(f"{redirect_path}?open={quote(new_show_id)}", "Show added.", kind="success")


@router.post("/events/{event_id}/shows/{show_id}/edit")
async def update_show_web(
    request: Request,
    event_id: str,
    show_id: str,
    principal: Principal = Depends(require_web_admin),
    csrf_token: str = Form(...),
    date: str = Form(...),
    doors_time: str = Form(...),
    start_time: str = Form(...),
    venue_name: str = Form(...),
    venue_address: str = Form(...),
    capacity: str = Form(...),
    status: str = Form(...),
    orig_date: str = Form(""),
    orig_doors_time: str = Form(""),
    orig_start_time: str = Form(""),
    orig_venue_name: str = Form(""),
    orig_venue_address: str = Form(""),
    orig_capacity: str = Form(""),
    orig_status: str = Form(""),
) -> RedirectResponse:
    """PATCH only the changed fields."""
    verify_csrf(request, csrf_token)
    redirect_path = f"/events/{event_id}/shows?open={quote(show_id)}"

    body: dict[str, Any] = {}
    if date != orig_date:
        body["date"] = date
    if doors_time != orig_doors_time:
        body["doors_time"] = doors_time
    if start_time != orig_start_time:
        body["start_time"] = start_time
    if venue_name != orig_venue_name:
        body["venue_name"] = venue_name
    if venue_address != orig_venue_address:
        body["venue_address"] = venue_address
    if status != orig_status:
        body["status"] = status
    if capacity != orig_capacity:
        capacity_int = _parse_int_or_none(capacity)
        if capacity_int is None:
            return redirect_with_flash(redirect_path, "Capacity must be a whole number.", kind="error")
        body["capacity"] = capacity_int

    if not body:
        return redirect_with_flash(redirect_path, "No changes to save.", kind="success")

    async with internal_api_client(request) as client:
        resp = await client.patch(f"/api/v1/events/{event_id}/shows/{show_id}", json=body)

    if resp.status_code == 404:
        return redirect_with_flash(redirect_path, "Show not found.", kind="error")
    if resp.status_code >= 400:
        return redirect_with_flash(redirect_path, api_error_detail(resp, "Could not update this show."), kind="error")
    return redirect_with_flash(redirect_path, "Show updated.", kind="success")


@router.post("/events/{event_id}/shows/{show_id}/duplicate")
async def duplicate_show_web(
    request: Request,
    event_id: str,
    show_id: str,
    principal: Principal = Depends(require_web_admin),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    """Duplicate the show (with ticket types) and open the new one's panel."""
    verify_csrf(request, csrf_token)
    redirect_path = f"/events/{event_id}/shows"

    async with internal_api_client(request) as client:
        resp = await client.post(f"/api/v1/events/{event_id}/shows/{show_id}/duplicate")

    if resp.status_code == 404:
        return redirect_with_flash(redirect_path, "Show not found.", kind="error")
    if resp.status_code >= 400:
        return redirect_with_flash(redirect_path, api_error_detail(resp, "Could not duplicate this show."), kind="error")

    new_show_id = resp.json().get("id")
    return redirect_with_flash(f"{redirect_path}?open={quote(new_show_id)}", "Show duplicated.", kind="success")


@router.post("/events/{event_id}/shows/{show_id}/delete")
async def delete_show_web(
    request: Request,
    event_id: str,
    show_id: str,
    principal: Principal = Depends(require_web_admin),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    """Delete the show; a 409 (sold tickets) is flashed. Guarded by a confirm dialog."""
    verify_csrf(request, csrf_token)
    redirect_path = f"/events/{event_id}/shows"

    async with internal_api_client(request) as client:
        resp = await client.delete(f"/api/v1/events/{event_id}/shows/{show_id}")

    if resp.status_code == 404:
        return redirect_with_flash(redirect_path, "Show not found.", kind="error")
    if resp.status_code >= 400:
        return redirect_with_flash(redirect_path, api_error_detail(resp, "Could not delete this show."), kind="error")
    return redirect_with_flash(redirect_path, "Show deleted.", kind="success")


@router.post("/events/{event_id}/shows/{show_id}/ticket-types")
async def create_ticket_type_web(
    request: Request,
    event_id: str,
    show_id: str,
    principal: Principal = Depends(require_web_admin),
    csrf_token: str = Form(...),
    name: str = Form(...),
    price: str = Form(...),
    quantity_available: str = Form(...),
    service_fee_included: bool = Form(True),
) -> RedirectResponse:
    """Create a ticket type. ``price`` is forwarded as a string: parsing to float
    first could introduce rounding errors into money.
    """
    verify_csrf(request, csrf_token)
    redirect_path = f"/events/{event_id}/shows?open={quote(show_id)}"

    quantity_int = _parse_int_or_none(quantity_available)
    if quantity_int is None:
        return redirect_with_flash(redirect_path, "Quantity available must be a whole number.", kind="error")

    body = {
        "name": name,
        "price": price,
        "quantity_available": quantity_int,
        "service_fee_included": service_fee_included,
    }
    async with internal_api_client(request) as client:
        resp = await client.post(f"/api/v1/shows/{show_id}/ticket-types", json=body)

    if resp.status_code == 404:
        return redirect_with_flash(redirect_path, "Show not found.", kind="error")
    if resp.status_code >= 400:
        return redirect_with_flash(
            redirect_path, api_error_detail(resp, "Could not add this ticket type."), kind="error"
        )
    return redirect_with_flash(redirect_path, "Ticket type added.", kind="success")


@router.post("/events/{event_id}/shows/{show_id}/ticket-types/{ticket_type_id}/edit")
async def update_ticket_type_web(
    request: Request,
    event_id: str,
    show_id: str,
    ticket_type_id: str,
    principal: Principal = Depends(require_web_admin),
    csrf_token: str = Form(...),
    name: str = Form(...),
    price: str = Form(...),
    quantity_available: str = Form(...),
    service_fee_included: bool = Form(False),
    orig_name: str = Form(""),
    orig_price: str = Form(""),
    orig_quantity_available: str = Form(""),
    orig_service_fee_included: str = Form("false"),
) -> RedirectResponse:
    """PATCH only the changed fields."""
    verify_csrf(request, csrf_token)
    redirect_path = f"/events/{event_id}/shows?open={quote(show_id)}"

    body: dict[str, Any] = {}
    if name != orig_name:
        body["name"] = name
    if price != orig_price:
        body["price"] = price
    if quantity_available != orig_quantity_available:
        quantity_int = _parse_int_or_none(quantity_available)
        if quantity_int is None:
            return redirect_with_flash(redirect_path, "Quantity available must be a whole number.", kind="error")
        body["quantity_available"] = quantity_int
    submitted_fee_flag = "true" if service_fee_included else "false"
    if submitted_fee_flag != orig_service_fee_included:
        body["service_fee_included"] = service_fee_included

    if not body:
        return redirect_with_flash(redirect_path, "No changes to save.", kind="success")

    async with internal_api_client(request) as client:
        resp = await client.patch(f"/api/v1/shows/{show_id}/ticket-types/{ticket_type_id}", json=body)

    if resp.status_code == 404:
        return redirect_with_flash(redirect_path, "Ticket type not found.", kind="error")
    if resp.status_code >= 400:
        return redirect_with_flash(
            redirect_path, api_error_detail(resp, "Could not update this ticket type."), kind="error"
        )
    return redirect_with_flash(redirect_path, "Ticket type updated.", kind="success")


@router.post("/events/{event_id}/shows/{show_id}/ticket-types/{ticket_type_id}/delete")
async def delete_ticket_type_web(
    request: Request,
    event_id: str,
    show_id: str,
    ticket_type_id: str,
    principal: Principal = Depends(require_web_admin),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    """Delete the ticket type; a 409 (sold tickets) is flashed. Guarded by a confirm dialog."""
    verify_csrf(request, csrf_token)
    redirect_path = f"/events/{event_id}/shows?open={quote(show_id)}"

    async with internal_api_client(request) as client:
        resp = await client.delete(f"/api/v1/shows/{show_id}/ticket-types/{ticket_type_id}")

    if resp.status_code == 404:
        return redirect_with_flash(redirect_path, "Ticket type not found.", kind="error")
    if resp.status_code >= 400:
        return redirect_with_flash(
            redirect_path, api_error_detail(resp, "Could not delete this ticket type."), kind="error"
        )
    return redirect_with_flash(redirect_path, "Ticket type deleted.", kind="success")


@router.post("/events/{event_id}/shows/{show_id}/manual-orders")
async def create_manual_order_web(
    request: Request,
    event_id: str,
    show_id: str,
    principal: Principal = Depends(require_web_admin),
) -> RedirectResponse:
    """"Issue tickets manually": create and settle an order for this show. Reads
    the raw form because the quantity fields are named ``qty_<ticket_type_id>``.
    """
    form = await request.form()
    form_data = {key: str(value) for key, value in form.multi_items()}
    verify_csrf(request, form_data.get("csrf_token", ""))
    redirect_path = f"/events/{event_id}/shows?open={quote(show_id)}"

    buyer_name = form_data.get("buyer_name", "").strip()
    if not buyer_name:
        return redirect_with_flash(redirect_path, "Buyer name is required.", kind="error")
    method_label = form_data.get("method_label", "").strip()
    if not method_label:
        return redirect_with_flash(redirect_path, "Payment method is required.", kind="error")
    buyer_email = form_data.get("buyer_email", "").strip() or None
    buyer_address = form_data.get("buyer_address", "").strip() or None
    reason = form_data.get("reason", "").strip() or None

    items: list[dict[str, Any]] = []
    for key, value in form_data.items():
        if not key.startswith("qty_"):
            continue
        qty = _parse_int_or_none(value)
        if qty is not None and qty > 0:
            items.append({"ticket_type_id": key.removeprefix("qty_"), "quantity": qty})

    if not items:
        return redirect_with_flash(redirect_path, "Select at least one ticket to issue.", kind="error")

    body: dict[str, Any] = {
        "buyer_name": buyer_name,
        "buyer_email": buyer_email,
        "buyer_address": buyer_address,
        "items": items,
        "method_label": method_label,
        "reason": reason,
    }
    async with internal_api_client(request) as client:
        resp = await client.post(f"/api/v1/shows/{show_id}/manual-orders", json=body)

    if resp.status_code == 404:
        return redirect_with_flash(redirect_path, "Show or ticket type not found.", kind="error")
    if resp.status_code >= 400:
        return redirect_with_flash(redirect_path, api_error_detail(resp, "Could not issue tickets."), kind="error")
    return redirect_with_flash(redirect_path, "Tickets issued.", kind="success")

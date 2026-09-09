"""Backoffice "Manage shows" page: Show CRUD plus, nested underneath each
Show, TicketType CRUD — the two are managed together on one page since a
TicketType only ever exists under a Show (see ``app.models.ticket_type.
TicketType``).

Fills the gap left by Milestone 1's Show/TicketType JSON API
(``app.api.routes.shows``, ``app.api.routes.ticket_types``): a full, tested
CRUD API has existed since then, but there has been no backoffice UI to
drive it, forcing raw API calls to add a Show or TicketType to an Event.

Same thin-proxy shape as every other backoffice web route module (see
``app.web.routes.themes``'s docstring): every mutation forwards to the
existing JSON API via ``app.web.api_client.internal_api_client``, so
validation (field constraints), integrity-conflict handling
(``app.api.routes._utils.commit_or_conflict``), and audit logging all
continue to live exactly once in ``app.api.routes.shows``/``ticket_types``.
This module only translates between HTML forms and that JSON API, and
renders templates.

Edit forms send true PATCH-with-only-changed-fields bodies (matching the
JSON API's ``exclude_unset`` semantics — see ``app.api.routes._utils.
apply_partial_update``) rather than always re-sending every field: each edit
form carries a hidden ``orig_<field>`` twin of every editable field,
populated from the same saved value the visible field was pre-filled with,
and the field is only included in the outgoing JSON body when the submitted
value differs from its ``orig_`` twin. This avoids touching a Show/TicketType
(and writing an audit-log entry) when the admin submits an edit form having
changed nothing, and avoids clobbering a field that another admin changed
concurrently between page load and this submit with a stale re-echoed value.
"""

from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from app.api.deps import Principal
from app.core.templating import templates
from app.web.api_client import internal_api_client
from app.web.csrf import attach_csrf_cookie, read_or_generate_csrf_token, verify_csrf
from app.web.deps import require_web_admin
from app.web.flash import redirect_with_flash

router = APIRouter(tags=["backoffice-shows"])

# Mirrors app.models.enums.PublishStatus, same (value, label) shape as
# app.web.routes.themes.STATUS_CHOICES — kept as a local copy rather than a
# shared import since it's presentation-only; the enum itself remains the
# single source of truth for which values are valid (enforced server-side).
STATUS_CHOICES = [
    ("draft", "Draft"),
    ("published", "Published"),
]


def _error_detail(response: httpx.Response, fallback: str) -> str:
    """Extract a human-readable message from a JSON API error response.

    Handles both shapes the API can return: a plain string ``detail`` (every
    hand-written ``HTTPException`` in this codebase, e.g. 404/409 responses),
    and FastAPI/Pydantic's own ``detail`` shape for a 422 validation error —
    a list of ``{"loc": [...], "msg": ..., ...}`` objects. Falls back to
    ``fallback`` for anything else (non-JSON body, unexpected shape), so a
    caller never has to worry about a raw/opaque error reaching the admin —
    per this milestone's brief: "surface that as a clear form-level error...
    don't just show a raw 422."
    """
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - response body may not be JSON at all
        return fallback
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        messages = []
        for err in detail:
            if not isinstance(err, dict):
                continue
            loc = [str(part) for part in err.get("loc", []) if part != "body"]
            field = ".".join(loc)
            msg = err.get("msg") or "Invalid value."
            messages.append(f"{field}: {msg}" if field else msg)
        if messages:
            return "; ".join(messages)
    return fallback


def _parse_int_or_none(raw: str) -> int | None:
    """Best-effort int parse for form fields backed by ``<input
    type="number">`` — the browser's own numeric validation is a UX nicety,
    not a security boundary, so a non-numeric submission (e.g. a crafted
    request bypassing the browser) is handled here rather than trusted."""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def _fetch_shows_context(request: Request, event_id: str) -> dict[str, Any]:
    """Gather the event and every Show under it, each augmented with its own
    list of TicketTypes (already carrying a live ``remaining`` count — see
    ``app.api.routes.ticket_types.list_ticket_types``, which calls
    ``app.services.stock.attach_remaining`` before responding).

    One API call for the event, one for its Shows, and one more per Show for
    that Show's TicketTypes — an N+1 shape, accepted here (over the internal
    ASGI transport, not a real network hop) the same way
    ``app.web.routes.themes._fetch_editor_context`` accepts several
    sequential calls per page: KISS over premature batching for a
    low-cardinality admin page.
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
                "shows_error": _error_detail(shows_response, "Could not load shows for this event."),
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
    """Render the combined Show + nested-TicketType management page for one
    Event. ``?open=<show_id>`` (set by every mutation route below on
    redirect) re-expands that Show's ``<details>`` panel after a full-page
    reload, so an admin editing one Show's ticket types isn't dropped back
    at a fully collapsed page after every save."""
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
    """Create a new Show under this Event. Proxies to
    ``POST /api/v1/events/{event_id}/shows`` (``app.api.routes.shows.
    create_show``) — every field constraint (e.g. ``capacity`` must be > 0)
    is enforced there; a 422 is translated into a plain-English flash message
    via :func:`_error_detail` rather than surfaced as a raw error page.

    Notably NOT enforced anywhere today, checked against ``app.schemas.show.
    ShowCreateRequest`` while building this route: that ``doors_time`` comes
    before ``start_time``. Nothing in this form second-guesses that — an
    out-of-order pair is accepted exactly as the API accepts it — but see
    this module's PR/handoff notes for a flag to reconsider adding that
    validation at the schema layer.
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
        return redirect_with_flash(redirect_path, _error_detail(resp, "Could not add this show."), kind="error")

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
    """Update a Show, sending only the fields that actually changed (see
    this module's docstring) as a PATCH to ``/api/v1/events/{event_id}/
    shows/{show_id}`` (``app.api.routes.shows.update_show``)."""
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
        return redirect_with_flash(redirect_path, _error_detail(resp, "Could not update this show."), kind="error")
    return redirect_with_flash(redirect_path, "Show updated.", kind="success")


@router.post("/events/{event_id}/shows/{show_id}/delete")
async def delete_show_web(
    request: Request,
    event_id: str,
    show_id: str,
    principal: Principal = Depends(require_web_admin),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    """Delete a Show. Proxies to ``DELETE /api/v1/events/{event_id}/shows/
    {show_id}`` (``app.api.routes.shows.delete_show``), which cascades to
    this Show's TicketTypes but fails with a clean 409 (via ``app.api.
    routes._utils.commit_or_conflict``) — surfaced here as a flash message,
    not a raw error page — if any of those TicketTypes still have purchased
    Tickets attached. Gated client-side by the shared ``data-confirm``
    native-``confirm()`` pattern (see ``backoffice/base.html``, and the GDPR-
    erasure actions in ``backoffice/orders_list.html`` for the precedent)
    since this is destructive and, when it succeeds, irreversible."""
    verify_csrf(request, csrf_token)
    redirect_path = f"/events/{event_id}/shows"

    async with internal_api_client(request) as client:
        resp = await client.delete(f"/api/v1/events/{event_id}/shows/{show_id}")

    if resp.status_code == 404:
        return redirect_with_flash(redirect_path, "Show not found.", kind="error")
    if resp.status_code >= 400:
        return redirect_with_flash(redirect_path, _error_detail(resp, "Could not delete this show."), kind="error")
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
    """Create a new TicketType under this Show. Proxies to ``POST /api/v1/
    shows/{show_id}/ticket-types`` (``app.api.routes.ticket_types.
    create_ticket_type``). ``price`` is forwarded as the submitted string
    rather than parsed into a Python float first — Pydantic's ``Decimal``
    field parses a numeric string directly, and round-tripping through
    ``float`` first risks introducing binary floating-point error into a
    money field for no benefit."""
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
            redirect_path, _error_detail(resp, "Could not add this ticket type."), kind="error"
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
    """Update a TicketType, sending only the fields that actually changed
    (see this module's docstring) as a PATCH to ``/api/v1/shows/{show_id}/
    ticket-types/{ticket_type_id}`` (``app.api.routes.ticket_types.
    update_ticket_type``)."""
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
            redirect_path, _error_detail(resp, "Could not update this ticket type."), kind="error"
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
    """Delete a TicketType. Proxies to ``DELETE /api/v1/shows/{show_id}/
    ticket-types/{ticket_type_id}`` (``app.api.routes.ticket_types.
    delete_ticket_type``), which — per that model's docstring
    (``app.models.ticket_type.TicketType``, ``ondelete="RESTRICT"`` on
    ``Ticket.ticket_type_id``) — fails with a clean 409 (via ``app.api.
    routes._utils.commit_or_conflict``), not a raw 500, once this TicketType
    has any purchased Tickets attached. That 409's message is surfaced here
    as a flash message verbatim. Gated client-side by the same shared
    ``data-confirm`` pattern as :func:`delete_show_web`."""
    verify_csrf(request, csrf_token)
    redirect_path = f"/events/{event_id}/shows?open={quote(show_id)}"

    async with internal_api_client(request) as client:
        resp = await client.delete(f"/api/v1/shows/{show_id}/ticket-types/{ticket_type_id}")

    if resp.status_code == 404:
        return redirect_with_flash(redirect_path, "Ticket type not found.", kind="error")
    if resp.status_code >= 400:
        return redirect_with_flash(
            redirect_path, _error_detail(resp, "Could not delete this ticket type."), kind="error"
        )
    return redirect_with_flash(redirect_path, "Ticket type deleted.", kind="success")

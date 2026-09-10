"""Backoffice Events pages: the index list, and full Event CRUD (create,
edit, delete).

Every mutation here is a thin proxy to the existing JSON API under
``app.api.routes.events`` via ``app.web.api_client`` — slug-uniqueness
checking, cascade-delete-vs-FK-restrict handling, and audit logging all
continue to live exactly once in that module. This file only translates
between HTML forms and that JSON API, and renders templates. Per-event
sub-pages (Theme, EventConfig, Email templates, Orders, Stats) each live in
their own ``app.web.routes.*`` module — this one owns only the Event entity
itself: the index list, the create form, the edit form (name/slug/
description/status/sales_paused, plus the preview-link copy affordance),
and delete.
"""

from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from app.api.deps import Principal
from app.core.config import get_settings
from app.core.templating import templates
from app.web.api_client import internal_api_client
from app.web.csrf import attach_csrf_cookie, read_or_generate_csrf_token, verify_csrf
from app.web.deps import require_web_admin
from app.web.flash import redirect_with_flash

router = APIRouter(tags=["backoffice-events"])

# Mirrors app.models.enums.PublishStatus — kept here only as (value, label)
# pairs for the <select> options, same convention as
# app.web.routes.themes.STATUS_CHOICES/FONT_CHOICES; the JSON API's
# EventCreateRequest/EventUpdateRequest enforce the real enum.
STATUS_CHOICES = [
    ("draft", "Draft"),
    ("published", "Published"),
]


def _error_detail(response: Any, fallback: str) -> str:
    """Same convention as every other web-route module's copy of this
    helper (``app.web.routes.orders``, ``app.web.routes.email_templates``,
    ``app.web.routes.themes``'s inline equivalent): FastAPI/Pydantic
    validation failures (422) return ``detail`` as a list of error objects,
    not a string — fall back to a generic message rather than rendering a
    Python list repr in the page."""
    try:
        detail = response.json().get("detail", fallback)
    except Exception:  # noqa: BLE001 - response body may not be JSON at all
        return fallback
    return detail if isinstance(detail, str) else fallback




@router.get("/events", response_model=None)
async def events_list(request: Request, principal: Principal = Depends(require_web_admin)) -> Response:
    """List all events, with links into each one's edit/theme/config pages."""
    async with internal_api_client(request) as client:
        events_response = await client.get("/api/v1/events")
    events = events_response.json()

    token = read_or_generate_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "backoffice/events_list.html",
        {
            "principal": principal,
            "events": events,
            "public_base_url": get_settings().public_base_url,
            "csrf_token": token,
            "flash": request.query_params.get("flash"),
            "flash_kind": request.query_params.get("flash_kind", "success"),
        },
    )
    attach_csrf_cookie(response, token)
    return response


@router.get("/events/new", response_model=None)
async def new_event_form(request: Request, principal: Principal = Depends(require_web_admin)) -> Response:
    """Render the blank "create event" form."""
    token = read_or_generate_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "backoffice/event_new.html",
        {
            "principal": principal,
            "status_choices": STATUS_CHOICES,
            "csrf_token": token,
            "flash": request.query_params.get("flash"),
            "flash_kind": request.query_params.get("flash_kind", "success"),
        },
    )
    attach_csrf_cookie(response, token)
    return response


@router.post("/events/new")
async def create_event(
    request: Request,
    principal: Principal = Depends(require_web_admin),
    name: str = Form(...),
    slug: str = Form(...),
    description: str = Form(""),
    status: str = Form("draft"),
    sales_paused: bool = Form(False),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    """Proxy to ``POST /api/v1/events`` (``app.api.routes.events.create_event``).

    On success, redirects straight into the new event's edit page rather
    than back to the events list — the edit page is also where its preview
    link lives, and is the natural next stop for an admin who just created
    an event with nothing else configured on it yet.
    """
    verify_csrf(request, csrf_token)
    body: dict[str, Any] = {
        "name": name.strip(),
        "slug": slug.strip(),
        "description": description.strip() or None,
        "status": status,
        "sales_paused": sales_paused,
    }

    async with internal_api_client(request) as client:
        resp = await client.post("/api/v1/events", json=body)

    if resp.status_code >= 400:
        return redirect_with_flash("/events/new", _error_detail(resp, "Could not create event."), kind="error")

    event = resp.json()
    return redirect_with_flash(f"/events/{event['id']}/edit", "Event created.", kind="success")


@router.get("/events/{event_id}/edit", response_model=None)
async def edit_event_form(
    request: Request, event_id: str, principal: Principal = Depends(require_web_admin)
) -> Response:
    """Render the "edit event" form, including the event's preview-link
    copy affordance (per PROJECT_BRIEF.md's Sharing section) and the
    delete-event danger zone."""
    async with internal_api_client(request) as client:
        event_response = await client.get(f"/api/v1/events/{event_id}")
    if event_response.status_code == 404:
        raise HTTPException(status_code=404, detail="Event not found.")
    event = event_response.json()

    token = read_or_generate_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "backoffice/event_edit.html",
        {
            "principal": principal,
            "event": event,
            "status_choices": STATUS_CHOICES,
            "public_base_url": get_settings().public_base_url,
            "csrf_token": token,
            "flash": request.query_params.get("flash"),
            "flash_kind": request.query_params.get("flash_kind", "success"),
        },
    )
    attach_csrf_cookie(response, token)
    return response


@router.post("/events/{event_id}/edit")
async def update_event(
    request: Request,
    event_id: str,
    principal: Principal = Depends(require_web_admin),
    name: str = Form(...),
    slug: str = Form(...),
    description: str = Form(""),
    status: str = Form(...),
    sales_paused: bool = Form(False),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    """Proxy to ``PATCH /api/v1/events/{event_id}``
    (``app.api.routes.events.update_event``).

    The API's ``EventUpdateRequest`` uses Pydantic ``exclude_unset``
    semantics — a field omitted from the JSON body is left completely
    untouched, distinct from being explicitly reset to its own current
    value. This form always submits every field (a full edit form, not a
    partial-diff UI), so this route diffs the submitted values against the
    event's own current state itself and sends only the fields that
    actually changed. This matters for two real reasons, not just tidiness:
    it keeps the PATCH route's audit-log ``detail`` payload limited to what
    genuinely changed, and it avoids re-triggering that route's
    slug-uniqueness check against a slug that's merely being resubmitted
    unchanged (a real event re-saving its own current slug should never be
    rejected as "already in use" by itself).
    """
    verify_csrf(request, csrf_token)
    redirect_path = f"/events/{event_id}/edit"

    async with internal_api_client(request) as client:
        current_response = await client.get(f"/api/v1/events/{event_id}")
        if current_response.status_code == 404:
            return redirect_with_flash("/events", "Event not found.", kind="error")
        current = current_response.json()

        changes: dict[str, Any] = {}
        new_description = description.strip() or None
        if name.strip() != current["name"]:
            changes["name"] = name.strip()
        if slug.strip() != current["slug"]:
            changes["slug"] = slug.strip()
        if new_description != current["description"]:
            changes["description"] = new_description
        if status != current["status"]:
            changes["status"] = status
        if sales_paused != current["sales_paused"]:
            changes["sales_paused"] = sales_paused

        if not changes:
            return redirect_with_flash(redirect_path, "No changes to save.", kind="success")

        resp = await client.patch(f"/api/v1/events/{event_id}", json=changes)

    if resp.status_code >= 400:
        return redirect_with_flash(redirect_path, _error_detail(resp, "Could not save event."), kind="error")
    return redirect_with_flash(redirect_path, "Event saved.", kind="success")


@router.post("/events/{event_id}/delete")
async def delete_event(
    request: Request,
    event_id: str,
    principal: Principal = Depends(require_web_admin),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    """Proxy to ``DELETE /api/v1/events/{event_id}``
    (``app.api.routes.events.delete_event``).

    Irreversible: cascade-deletes the event's EventConfig, Theme, Shows, and
    TicketTypes — but ``Ticket.ticket_type_id`` uses ``ON DELETE RESTRICT``
    (see that model's docstring), so a TicketType with real purchased
    Tickets under it blocks the whole deletion at the database level. The
    API route translates that DB integrity error into a clean 409 via
    ``app.api.routes._utils.commit_or_conflict`` rather than a raw 500;
    this route surfaces that 409's message as a flash on the edit page
    rather than silently failing or crashing. Gated client-side by the
    shared ``data-confirm`` submit-guard already established by the GDPR
    buyer-PII-erasure UI (see ``backoffice/base.html`` and
    ``backoffice/orders_list.html``) — the template's delete form names the
    event explicitly in its confirmation text.
    """
    verify_csrf(request, csrf_token)
    async with internal_api_client(request) as client:
        resp = await client.delete(f"/api/v1/events/{event_id}")

    if resp.status_code == 404:
        return redirect_with_flash("/events", "Event not found.", kind="error")
    if resp.status_code == 409:
        return redirect_with_flash(
            f"/events/{event_id}/edit",
            _error_detail(resp, "This event has ticket types with existing orders and cannot be deleted."),
            kind="error",
        )
    if resp.status_code >= 400:
        return redirect_with_flash(
            f"/events/{event_id}/edit", _error_detail(resp, "Could not delete event."), kind="error"
        )
    return redirect_with_flash("/events", "Event deleted.", kind="success")


@router.post("/events/{event_id}/set-default")
async def set_default_event(
    request: Request,
    event_id: str,
    principal: Principal = Depends(require_web_admin),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    """Mark this Event as the default (see ``app.web.routes.homepage`` and
    ``app.api.routes.events.set_default_event``'s docstring) — proxies to
    ``POST /api/v1/events/{event_id}/set-default``, which also clears any
    previously-default Event in the same transaction."""
    verify_csrf(request, csrf_token)
    async with internal_api_client(request) as client:
        resp = await client.post(f"/api/v1/events/{event_id}/set-default")

    if resp.status_code == 404:
        return redirect_with_flash("/events", "Event not found.", kind="error")
    if resp.status_code >= 400:
        return redirect_with_flash("/events", _error_detail(resp, "Could not set default event."), kind="error")
    return redirect_with_flash("/events", "Default event set.", kind="success")


@router.post("/events/{event_id}/unset-default")
async def unset_default_event(
    request: Request,
    event_id: str,
    principal: Principal = Depends(require_web_admin),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    """Clear this Event's default-Event status. Proxies to ``POST /api/v1/
    events/{event_id}/unset-default`` — idempotent, same as that route."""
    verify_csrf(request, csrf_token)
    async with internal_api_client(request) as client:
        resp = await client.post(f"/api/v1/events/{event_id}/unset-default")

    if resp.status_code == 404:
        return redirect_with_flash("/events", "Event not found.", kind="error")
    if resp.status_code >= 400:
        return redirect_with_flash("/events", _error_detail(resp, "Could not unset default event."), kind="error")
    return redirect_with_flash("/events", "Default event unset.", kind="success")

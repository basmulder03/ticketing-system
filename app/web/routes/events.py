"""Backoffice event pages: list, create, edit (incl. preview link), delete, and
default-event toggles. Other per-event pages live in their own modules.
"""

from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from app.api.deps import Principal
from app.core.config import get_settings
from app.core.templating import templates
from app.web.api_client import api_error_detail, internal_api_client
from app.web.csrf import attach_csrf_cookie, read_or_generate_csrf_token, verify_csrf
from app.web.deps import require_web_admin
from app.web.flash import redirect_with_flash

router = APIRouter(tags=["backoffice-events"])

# Select options; the API enforces the real enum.
STATUS_CHOICES = [
    ("draft", "Draft"),
    ("published", "Published"),
]


@router.get("/events", response_model=None)
async def events_list(request: Request, principal: Principal = Depends(require_web_admin)) -> Response:
    """All events, with links to each one's pages."""
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
    """The blank create form."""
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
    """Create, then go to the new event's edit page (where its preview link is)."""
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
        return redirect_with_flash("/events/new", api_error_detail(resp, "Could not create event."), kind="error")

    event = resp.json()
    return redirect_with_flash(f"/events/{event['id']}/edit", "Event created.", kind="success")


@router.get("/events/{event_id}/edit", response_model=None)
async def edit_event_form(
    request: Request, event_id: str, principal: Principal = Depends(require_web_admin)
) -> Response:
    """The edit form, with the preview link and delete option."""
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
    """Send only changed fields. Keeps the audit entry accurate and avoids
    re-checking an unchanged slug against itself.
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
        return redirect_with_flash(redirect_path, api_error_detail(resp, "Could not save event."), kind="error")
    return redirect_with_flash(redirect_path, "Event saved.", kind="success")


@router.post("/events/{event_id}/delete")
async def delete_event(
    request: Request,
    event_id: str,
    principal: Principal = Depends(require_web_admin),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    """Delete the event and everything under it; a 409 (sold tickets) is shown as
    a flash. Guarded by a confirm dialog naming the event.
    """
    verify_csrf(request, csrf_token)
    async with internal_api_client(request) as client:
        resp = await client.delete(f"/api/v1/events/{event_id}")

    if resp.status_code == 404:
        return redirect_with_flash("/events", "Event not found.", kind="error")
    if resp.status_code == 409:
        return redirect_with_flash(
            f"/events/{event_id}/edit",
            api_error_detail(resp, "This event has ticket types with existing orders and cannot be deleted."),
            kind="error",
        )
    if resp.status_code >= 400:
        return redirect_with_flash(
            f"/events/{event_id}/edit", api_error_detail(resp, "Could not delete event."), kind="error"
        )
    return redirect_with_flash("/events", "Event deleted.", kind="success")


@router.post("/events/{event_id}/set-default")
async def set_default_event(
    request: Request,
    event_id: str,
    principal: Principal = Depends(require_web_admin),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    """Make this the default event (clears any previous default)."""
    verify_csrf(request, csrf_token)
    async with internal_api_client(request) as client:
        resp = await client.post(f"/api/v1/events/{event_id}/set-default")

    if resp.status_code == 404:
        return redirect_with_flash("/events", "Event not found.", kind="error")
    if resp.status_code >= 400:
        return redirect_with_flash("/events", api_error_detail(resp, "Could not set default event."), kind="error")
    return redirect_with_flash("/events", "Default event set.", kind="success")


@router.post("/events/{event_id}/unset-default")
async def unset_default_event(
    request: Request,
    event_id: str,
    principal: Principal = Depends(require_web_admin),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    """Clear default status. Idempotent."""
    verify_csrf(request, csrf_token)
    async with internal_api_client(request) as client:
        resp = await client.post(f"/api/v1/events/{event_id}/unset-default")

    if resp.status_code == 404:
        return redirect_with_flash("/events", "Event not found.", kind="error")
    if resp.status_code >= 400:
        return redirect_with_flash("/events", api_error_detail(resp, "Could not unset default event."), kind="error")
    return redirect_with_flash("/events", "Default event unset.", kind="success")

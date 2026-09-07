"""Backoffice Theme editor page: fixed fields, logo/background upload,
advanced custom-CSS override, live preview, AA contrast report,
draft/published status, and "duplicate theme from previous event".

Every mutation (save, image upload/delete, copy-from, preview) is a thin
proxy to the existing JSON API under ``app.api.routes.themes`` via
``app.web.api_client`` — hex-color validation, CSS sanitization, AA
contrast calculation, image format sniffing, and audit logging all
continue to live exactly once in that module and the services it calls.
This file only translates between HTML forms and that JSON API, and
renders templates.
"""

from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from app.api.deps import Principal
from app.core.templating import templates
from app.web.api_client import internal_api_client
from app.web.csrf import attach_csrf_cookie, read_or_generate_csrf_token, verify_csrf
from app.web.deps import require_web_admin

router = APIRouter(tags=["backoffice-theme"])

# Mirrors app.models.enums.ThemeFont — kept here only as (value, label) pairs
# for the <select> options; the enum itself remains the single source of
# truth for which values are valid (enforced server-side by the JSON API).
FONT_CHOICES = [
    ("system-sans", "System Sans-serif"),
    ("system-serif", "System Serif"),
    ("inter", "Inter"),
    ("roboto", "Roboto"),
    ("open-sans", "Open Sans"),
    ("lora", "Lora"),
    ("merriweather", "Merriweather"),
    ("playfair-display", "Playfair Display"),
]

# Mirrors app.models.enums.PublishStatus.
STATUS_CHOICES = [
    ("draft", "Draft"),
    ("published", "Published"),
]

# Defaults mirror app.models.theme.Theme's column defaults, used only to
# pre-fill the form when an event has no Theme row yet (PUT creates one).
_DEFAULT_PRIMARY = "#1a1a1a"
_DEFAULT_SECONDARY = "#ffffff"
_DEFAULT_ACCENT = "#c9a227"


def _redirect_with_flash(path: str, message: str, kind: str = "success") -> RedirectResponse:
    return RedirectResponse(url=f"{path}?flash={quote(message)}&flash_kind={kind}", status_code=303)


def _build_preview_doc(preview: dict[str, Any]) -> str:
    """Wrap a ``ThemePreviewResponse``'s ``preview_css``/``sample_html`` into
    a tiny standalone HTML document for the preview ``<iframe>``. Embedding
    this string into the template via ``srcdoc="{{ preview_doc }}"`` relies
    on Jinja's default HTML auto-escaping to make it a safe attribute value
    — the browser un-escapes it back into the literal document when
    rendering the iframe, so no ``|safe`` filter is needed or used here.
    Safe to embed as raw HTML in the first place because ``sample_html`` is
    a fixed, non-user-controlled string and ``preview_css`` has already
    been through ``sanitize_custom_css`` server-side.
    """
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<style>{preview['preview_css']}</style></head>"
        f"<body>{preview['sample_html']}</body></html>"
    )


async def _fetch_editor_context(request: Request, event_id: str) -> dict[str, Any]:
    """Gather everything the theme editor page needs in one place: the
    event, its theme (if any), other events (for "duplicate from"), and an
    initial live-preview render using the currently saved (or default)
    values."""
    async with internal_api_client(request) as client:
        event_response = await client.get(f"/api/v1/events/{event_id}")
        if event_response.status_code == 404:
            raise HTTPException(status_code=404, detail="Event not found.")
        event = event_response.json()

        theme_response = await client.get(f"/api/v1/events/{event_id}/theme")
        theme = theme_response.json() if theme_response.status_code == 200 else None

        all_events_response = await client.get("/api/v1/events")
        other_events = [e for e in all_events_response.json() if e["id"] != event_id]

        preview_payload = {
            "primary_color": theme["primary_color"] if theme else _DEFAULT_PRIMARY,
            "secondary_color": theme["secondary_color"] if theme else _DEFAULT_SECONDARY,
            "accent_color": theme["accent_color"] if theme else _DEFAULT_ACCENT,
            "font_choice": theme["font_choice"] if theme else "system-sans",
            "custom_css": theme["custom_css"] if theme else None,
        }
        preview_response = await client.post(f"/api/v1/events/{event_id}/theme/preview", json=preview_payload)

    preview = preview_response.json() if preview_response.status_code == 200 else None
    preview_error = None if preview else preview_response.json().get("detail", "Could not build preview.")

    return {
        "event": event,
        "theme": theme,
        "other_events": other_events,
        "preview": preview,
        "preview_doc": _build_preview_doc(preview) if preview else None,
        "error": preview_error,
    }


@router.get("/events/{event_id}/theme", response_model=None)
async def theme_editor(
    request: Request, event_id: str, principal: Principal = Depends(require_web_admin)
) -> Response:
    ctx = await _fetch_editor_context(request, event_id)
    token = read_or_generate_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "backoffice/theme_editor.html",
        {
            "principal": principal,
            "event": ctx["event"],
            "theme": ctx["theme"],
            "other_events": ctx["other_events"],
            "preview": ctx["preview"],
            "preview_doc": ctx["preview_doc"],
            "error": ctx["error"],
            "font_choices": FONT_CHOICES,
            "status_choices": STATUS_CHOICES,
            "csrf_token": token,
            "flash": request.query_params.get("flash"),
            "flash_kind": request.query_params.get("flash_kind", "success"),
        },
    )
    attach_csrf_cookie(response, token)
    return response


@router.post("/events/{event_id}/theme/preview-fragment", response_class=HTMLResponse, response_model=None)
async def theme_preview_fragment(
    request: Request,
    event_id: str,
    principal: Principal = Depends(require_web_admin),
    primary_color: str = Form(...),
    secondary_color: str = Form(...),
    accent_color: str = Form(...),
    font_choice: str = Form(...),
    custom_css: str = Form(""),
    csrf_token: str = Form(...),
) -> Response:
    """HTMX target: re-renders the live-preview pane from the current
    (unsaved) form values, including whatever the CSS sanitizer strips —
    called on every debounced field change, never on full form submit."""
    verify_csrf(request, csrf_token)

    async with internal_api_client(request) as client:
        preview_response = await client.post(
            f"/api/v1/events/{event_id}/theme/preview",
            json={
                "primary_color": primary_color,
                "secondary_color": secondary_color,
                "accent_color": accent_color,
                "font_choice": font_choice,
                "custom_css": custom_css or None,
            },
        )

    if preview_response.status_code != 200:
        detail = preview_response.json().get("detail", "Enter valid colors to preview.")
        return templates.TemplateResponse(
            request, "backoffice/_theme_preview_fragment.html", {"preview": None, "error": detail}
        )

    preview = preview_response.json()
    return templates.TemplateResponse(
        request,
        "backoffice/_theme_preview_fragment.html",
        {
            "preview": preview,
            "preview_doc": _build_preview_doc(preview),
            "error": None,
        },
    )


@router.post("/events/{event_id}/theme")
async def save_theme_fields(
    request: Request,
    event_id: str,
    principal: Principal = Depends(require_web_admin),
    primary_color: str = Form(...),
    secondary_color: str = Form(...),
    accent_color: str = Form(...),
    font_choice: str = Form(...),
    custom_css: str = Form(""),
    status: str = Form(...),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    verify_csrf(request, csrf_token)
    async with internal_api_client(request) as client:
        put_response = await client.put(
            f"/api/v1/events/{event_id}/theme",
            json={
                "primary_color": primary_color,
                "secondary_color": secondary_color,
                "accent_color": accent_color,
                "font_choice": font_choice,
                "custom_css": custom_css or None,
                "status": status,
            },
        )

    if put_response.status_code >= 400:
        detail = put_response.json().get("detail", "Could not save theme.")
        return _redirect_with_flash(f"/events/{event_id}/theme", detail, kind="error")
    return _redirect_with_flash(f"/events/{event_id}/theme", "Theme saved.", kind="success")


async def _forward_upload(request: Request, url: str, upload: UploadFile) -> httpx.Response:
    content = await upload.read()
    async with internal_api_client(request) as client:
        return await client.put(
            url,
            files={"file": (upload.filename or "upload", content, upload.content_type or "application/octet-stream")},
        )


@router.post("/events/{event_id}/theme/logo")
async def upload_logo(
    request: Request,
    event_id: str,
    principal: Principal = Depends(require_web_admin),
    file: UploadFile = File(...),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    verify_csrf(request, csrf_token)
    resp = await _forward_upload(request, f"/api/v1/events/{event_id}/theme/logo", file)
    if resp.status_code >= 400:
        return _redirect_with_flash(
            f"/events/{event_id}/theme", resp.json().get("detail", "Logo upload failed."), kind="error"
        )
    return _redirect_with_flash(f"/events/{event_id}/theme", "Logo uploaded.", kind="success")


@router.post("/events/{event_id}/theme/logo/delete")
async def delete_logo(
    request: Request, event_id: str, principal: Principal = Depends(require_web_admin), csrf_token: str = Form(...)
) -> RedirectResponse:
    verify_csrf(request, csrf_token)
    async with internal_api_client(request) as client:
        resp = await client.delete(f"/api/v1/events/{event_id}/theme/logo")
    if resp.status_code >= 400:
        return _redirect_with_flash(
            f"/events/{event_id}/theme", resp.json().get("detail", "Could not remove logo."), kind="error"
        )
    return _redirect_with_flash(f"/events/{event_id}/theme", "Logo removed.", kind="success")


@router.post("/events/{event_id}/theme/background")
async def upload_background(
    request: Request,
    event_id: str,
    principal: Principal = Depends(require_web_admin),
    file: UploadFile = File(...),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    verify_csrf(request, csrf_token)
    resp = await _forward_upload(request, f"/api/v1/events/{event_id}/theme/background", file)
    if resp.status_code >= 400:
        return _redirect_with_flash(
            f"/events/{event_id}/theme", resp.json().get("detail", "Background upload failed."), kind="error"
        )
    return _redirect_with_flash(f"/events/{event_id}/theme", "Background image uploaded.", kind="success")


@router.post("/events/{event_id}/theme/background/delete")
async def delete_background(
    request: Request, event_id: str, principal: Principal = Depends(require_web_admin), csrf_token: str = Form(...)
) -> RedirectResponse:
    verify_csrf(request, csrf_token)
    async with internal_api_client(request) as client:
        resp = await client.delete(f"/api/v1/events/{event_id}/theme/background")
    if resp.status_code >= 400:
        return _redirect_with_flash(
            f"/events/{event_id}/theme", resp.json().get("detail", "Could not remove background."), kind="error"
        )
    return _redirect_with_flash(f"/events/{event_id}/theme", "Background image removed.", kind="success")


@router.post("/events/{event_id}/theme/copy-from")
async def copy_theme_from(
    request: Request,
    event_id: str,
    principal: Principal = Depends(require_web_admin),
    source_event_id: str = Form(...),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    verify_csrf(request, csrf_token)
    async with internal_api_client(request) as client:
        resp = await client.post(f"/api/v1/events/{event_id}/theme/copy-from/{source_event_id}")
    if resp.status_code >= 400:
        return _redirect_with_flash(
            f"/events/{event_id}/theme", resp.json().get("detail", "Could not copy theme."), kind="error"
        )
    return _redirect_with_flash(f"/events/{event_id}/theme", "Theme copied from the selected event.", kind="success")

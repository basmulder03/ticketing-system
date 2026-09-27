"""Backoffice email-template editor: subject/body per language, a placeholder
reference, and a debounced HTMX live preview using the event's real theme.

Only the order-confirmation template has an editor. The door-reservation
email is sent too, but its template is currently editable only via the API.
"""

from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from app.api.deps import Principal
from app.core.templating import templates
from app.i18n import translate
from app.web.api_client import api_error_detail, internal_api_client
from app.web.csrf import attach_csrf_cookie, read_or_generate_csrf_token, verify_csrf
from app.web.deps import require_web_admin
from app.web.flash import redirect_with_flash

router = APIRouter(tags=["backoffice-email-templates"])

# The only type with an editor (see the module docstring).
_TEMPLATE_TYPE = "order_confirmation_ticket"

# Language tabs; the API validates the codes.
_LANGUAGES = [("en", "English"), ("nl", "Nederlands")]
_LANGUAGE_CODES = {code for code, _ in _LANGUAGES}

# Pre-fill for uncustomized languages, from the same i18n keys the sender
# uses as its default (one source, so they can't drift).
def _default_subject(language: str) -> str:
    return translate("email.order_confirmation.subject", language)


def _default_body(language: str) -> str:
    return translate("email.order_confirmation.body", language)


# Must match app.services.email_render.build_placeholder_values' keys.
PLACEHOLDERS: list[tuple[str, str]] = [
    ("buyer_name", "The buyer's full name, e.g. \"Jamie Smith\"."),
    ("event_name", "The event's name, e.g. \"Christmas Passion\"."),
    ("show_date", "The show's date, formatted for the order's language (e.g. \"18 december 2026\" in Dutch)."),
    (
        "show_time",
        (
            "The show's start time, formatted for the order's language "
            "(e.g. \"20:00\" in Dutch, \"8:00 PM\" in English)."
        ),
    ),
    ("venue_name", "The venue's name, e.g. \"Het Kruispunt\"."),
    ("venue_address", "The venue's full address, as entered on the show."),
    (
        "order_total",
        "The order's total amount, formatted as currency for the order's language (e.g. \"€ 42,50\").",
    ),
    (
        "days_until_show",
        (
            "Days remaining until the show. Recalculated fresh every time this email is sent or "
            "resent, so a resend closer to the date always shows the correct, smaller number."
        ),
    ),
]


def _normalize_language(language: str | None) -> str:
    normalized = (language or "en").lower()
    return normalized if normalized in _LANGUAGE_CODES else "en"


async def _fetch_editor_context(request: Request, event_id: str, language: str) -> dict[str, Any]:
    """The event, its saved template (if any), and an initial preview."""
    async with internal_api_client(request) as client:
        event_response = await client.get(f"/api/v1/events/{event_id}")
        if event_response.status_code == 404:
            raise HTTPException(status_code=404, detail="Event not found.")
        event = event_response.json()

        template_response = await client.get(
            f"/api/v1/events/{event_id}/email-templates/{_TEMPLATE_TYPE}/{language}"
        )
        template = template_response.json() if template_response.status_code == 200 else None

        subject_value = template["subject"] if template else _default_subject(language)
        body_value = template["body"] if template else _default_body(language)

        preview_response = await client.post(
            f"/api/v1/events/{event_id}/email-templates/preview",
            json={
                "template_type": _TEMPLATE_TYPE,
                "language": language,
                "subject": subject_value,
                "body": body_value,
            },
        )

    preview = preview_response.json() if preview_response.status_code == 200 else None
    preview_error = None if preview else api_error_detail(preview_response, "Could not build preview.")

    return {
        "event": event,
        "template": template,
        "subject_value": subject_value,
        "body_value": body_value,
        "preview": preview,
        "preview_doc": preview["html_body"] if preview else None,
        "error": preview_error,
    }


@router.get("/events/{event_id}/email-templates", response_model=None)
async def email_template_editor(
    request: Request,
    event_id: str,
    language: str = "en",
    principal: Principal = Depends(require_web_admin),
) -> Response:
    language = _normalize_language(language)
    ctx = await _fetch_editor_context(request, event_id, language)
    token = read_or_generate_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "backoffice/email_template_editor.html",
        {
            "principal": principal,
            "event": ctx["event"],
            "template": ctx["template"],
            "language": language,
            "languages": _LANGUAGES,
            "subject_value": ctx["subject_value"],
            "body_value": ctx["body_value"],
            "placeholders": PLACEHOLDERS,
            "preview": ctx["preview"],
            "preview_doc": ctx["preview_doc"],
            "error": ctx["error"],
            "csrf_token": token,
            "flash": request.query_params.get("flash"),
            "flash_kind": request.query_params.get("flash_kind", "success"),
        },
    )
    attach_csrf_cookie(response, token)
    return response


@router.post(
    "/events/{event_id}/email-templates/preview-fragment",
    response_class=HTMLResponse,
    response_model=None,
)
async def email_template_preview_fragment(
    request: Request,
    event_id: str,
    principal: Principal = Depends(require_web_admin),
    language: str = Form(...),
    subject: str = Form(""),
    body: str = Form(""),
    csrf_token: str = Form(...),
) -> Response:
    """HTMX: re-render the preview from unsaved values on each debounced change."""
    verify_csrf(request, csrf_token)
    language = _normalize_language(language)

    async with internal_api_client(request) as client:
        preview_response = await client.post(
            f"/api/v1/events/{event_id}/email-templates/preview",
            json={
                "template_type": _TEMPLATE_TYPE,
                "language": language,
                "subject": subject,
                "body": body,
            },
        )

    if preview_response.status_code != 200:
        detail = api_error_detail(preview_response, "Enter a subject and body to preview.")
        return templates.TemplateResponse(
            request,
            "backoffice/_email_template_preview_fragment.html",
            {"preview": None, "error": detail},
        )

    preview = preview_response.json()
    return templates.TemplateResponse(
        request,
        "backoffice/_email_template_preview_fragment.html",
        {"preview": preview, "preview_doc": preview["html_body"], "error": None},
    )


@router.post("/events/{event_id}/email-templates")
async def save_email_template(
    request: Request,
    event_id: str,
    principal: Principal = Depends(require_web_admin),
    language: str = Form(...),
    subject: str = Form(...),
    body: str = Form(...),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    verify_csrf(request, csrf_token)
    language = _normalize_language(language)
    async with internal_api_client(request) as client:
        put_response = await client.put(
            f"/api/v1/events/{event_id}/email-templates/{_TEMPLATE_TYPE}/{language}",
            json={"subject": subject, "body": body},
        )

    redirect_path = f"/events/{event_id}/email-templates?language={language}"
    if put_response.status_code >= 400:
        return redirect_with_flash(
            redirect_path,
            api_error_detail(put_response, "Could not save email template."),
            kind="error",
        )
    return redirect_with_flash(redirect_path, "Email template saved.", kind="success")


@router.post("/events/{event_id}/email-templates/reset")
async def reset_email_template(
    request: Request,
    event_id: str,
    principal: Principal = Depends(require_web_admin),
    language: str = Form(...),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    """Delete the customization so sends (and this editor) revert to the default."""
    verify_csrf(request, csrf_token)
    language = _normalize_language(language)
    async with internal_api_client(request) as client:
        resp = await client.delete(
            f"/api/v1/events/{event_id}/email-templates/{_TEMPLATE_TYPE}/{language}"
        )

    redirect_path = f"/events/{event_id}/email-templates?language={language}"
    if resp.status_code >= 400 and resp.status_code != 404:
        return redirect_with_flash(
            redirect_path, api_error_detail(resp, "Could not reset email template."), kind="error"
        )
    return redirect_with_flash(
        redirect_path, "Reverted to the built-in default template.", kind="success"
    )

"""Backoffice email-template editor: subject/body per template type and
language, a placeholder reference, and a debounced HTMX live preview using
the event's real theme.
"""

from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from app.api.deps import Principal
from app.core.templating import templates
from app.i18n import translate
from app.models.enums import EmailTemplateType
from app.web.api_client import api_error_detail, internal_api_client
from app.web.csrf import attach_csrf_cookie, read_or_generate_csrf_token, verify_csrf
from app.web.deps import require_web_admin
from app.web.flash import redirect_with_flash

router = APIRouter(tags=["backoffice-email-templates"])

# Template-type tabs; the API validates the values.
_TEMPLATE_TYPES = [
    (EmailTemplateType.ORDER_CONFIRMATION_TICKET.value, "Order confirmation"),
    (EmailTemplateType.DOOR_PAYMENT_CONFIRMATION.value, "Door payment confirmation"),
]
_TEMPLATE_TYPE_VALUES = {value for value, _ in _TEMPLATE_TYPES}
_DEFAULT_TEMPLATE_TYPE = EmailTemplateType.ORDER_CONFIRMATION_TICKET.value

# The email.<prefix>.subject/body i18n keys don't share the enum's own
# spelling (see app/i18n/en.json) — this is the one place that maps between them.
_I18N_KEY_PREFIX_BY_TEMPLATE_TYPE = {
    EmailTemplateType.ORDER_CONFIRMATION_TICKET.value: "order_confirmation",
    EmailTemplateType.DOOR_PAYMENT_CONFIRMATION.value: "door_confirmation",
}

_DESCRIPTION_BY_TEMPLATE_TYPE = {
    # Only the order-confirmation email is resendable (from this event's Orders
    # page, for paid orders) — the door-reservation email sends exactly once,
    # right after checkout, so its description doesn't promise that.
    EmailTemplateType.ORDER_CONFIRMATION_TICKET.value: "sent automatically once an order is paid.",
    EmailTemplateType.DOOR_PAYMENT_CONFIRMATION.value: (
        "sent automatically right after checkout for a pay-at-the-door order, before any payment — "
        "it's a reservation summary, never real scannable tickets."
    ),
}

# Language tabs; the API validates the codes.
_LANGUAGES = [("en", "English"), ("nl", "Nederlands")]
_LANGUAGE_CODES = {code for code, _ in _LANGUAGES}


def _normalize_template_type(template_type: str | None) -> str:
    return template_type if template_type in _TEMPLATE_TYPE_VALUES else _DEFAULT_TEMPLATE_TYPE


# Pre-fill for uncustomized languages, from the same i18n keys the sender
# uses as its default (one source, so they can't drift).
def _default_subject(template_type: str, language: str) -> str:
    prefix = _I18N_KEY_PREFIX_BY_TEMPLATE_TYPE[template_type]
    return translate(f"email.{prefix}.subject", language)


def _default_body(template_type: str, language: str) -> str:
    prefix = _I18N_KEY_PREFIX_BY_TEMPLATE_TYPE[template_type]
    return translate(f"email.{prefix}.body", language)


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


async def _fetch_editor_context(request: Request, event_id: str, template_type: str, language: str) -> dict[str, Any]:
    """The event, its saved template (if any), and an initial preview."""
    async with internal_api_client(request) as client:
        event_response = await client.get(f"/api/v1/events/{event_id}")
        if event_response.status_code == 404:
            raise HTTPException(status_code=404, detail="Event not found.")
        event = event_response.json()

        template_response = await client.get(
            f"/api/v1/events/{event_id}/email-templates/{template_type}/{language}"
        )
        template = template_response.json() if template_response.status_code == 200 else None

        subject_value = template["subject"] if template else _default_subject(template_type, language)
        body_value = template["body"] if template else _default_body(template_type, language)

        preview_response = await client.post(
            f"/api/v1/events/{event_id}/email-templates/preview",
            json={
                "template_type": template_type,
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
    template_type: str = _DEFAULT_TEMPLATE_TYPE,
    language: str = "en",
    principal: Principal = Depends(require_web_admin),
) -> Response:
    template_type = _normalize_template_type(template_type)
    language = _normalize_language(language)
    ctx = await _fetch_editor_context(request, event_id, template_type, language)
    token = read_or_generate_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "backoffice/email_template_editor.html",
        {
            "principal": principal,
            "event": ctx["event"],
            "template": ctx["template"],
            "template_type": template_type,
            "template_types": _TEMPLATE_TYPES,
            "template_type_description": _DESCRIPTION_BY_TEMPLATE_TYPE[template_type],
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
    template_type: str = Form(...),
    language: str = Form(...),
    subject: str = Form(""),
    body: str = Form(""),
    csrf_token: str = Form(...),
) -> Response:
    """HTMX: re-render the preview from unsaved values on each debounced change."""
    verify_csrf(request, csrf_token)
    template_type = _normalize_template_type(template_type)
    language = _normalize_language(language)

    async with internal_api_client(request) as client:
        preview_response = await client.post(
            f"/api/v1/events/{event_id}/email-templates/preview",
            json={
                "template_type": template_type,
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
    template_type: str = Form(...),
    language: str = Form(...),
    subject: str = Form(...),
    body: str = Form(...),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    verify_csrf(request, csrf_token)
    template_type = _normalize_template_type(template_type)
    language = _normalize_language(language)
    async with internal_api_client(request) as client:
        put_response = await client.put(
            f"/api/v1/events/{event_id}/email-templates/{template_type}/{language}",
            json={"subject": subject, "body": body},
        )

    redirect_path = f"/events/{event_id}/email-templates?template_type={template_type}&language={language}"
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
    template_type: str = Form(...),
    language: str = Form(...),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    """Delete the customization so sends (and this editor) revert to the default."""
    verify_csrf(request, csrf_token)
    template_type = _normalize_template_type(template_type)
    language = _normalize_language(language)
    async with internal_api_client(request) as client:
        resp = await client.delete(
            f"/api/v1/events/{event_id}/email-templates/{template_type}/{language}"
        )

    redirect_path = f"/events/{event_id}/email-templates?template_type={template_type}&language={language}"
    if resp.status_code >= 400 and resp.status_code != 404:
        return redirect_with_flash(
            redirect_path, api_error_detail(resp, "Could not reset email template."), kind="error"
        )
    return redirect_with_flash(
        redirect_path, "Reverted to the built-in default template.", kind="success"
    )

"""Backoffice EmailTemplate editor: subject/body per language for the
``order_confirmation_ticket`` type, an "available placeholders" reference
list, and a debounced HTMX live preview rendered against sample data and
the event's REAL theme (mirrors ``app.web.routes.themes``'s live-preview
pattern) before saving — PROJECT_BRIEF.md's Ticket Generation & Delivery
section: "a preview using real theme colors/logo before saving".

Only ``order_confirmation_ticket`` gets an editor: it is the only
``EmailTemplateType`` wired to an actual send path in Milestone 4 (see
``app.services.ticket_delivery``) — other types the enum could one day grow
(invoice, door-payment reminder) are deliberately not exposed here, per
KISS ("don't build UI for types nothing sends yet").

Every mutation is a thin proxy to the existing JSON API under
``app.api.routes.email_templates`` via ``app.web.api_client`` — placeholder
substitution, HTML sanitization/escaping, and audit logging all continue to
live exactly once in that module and the services it calls. This file only
translates between HTML forms and that JSON API, and renders templates.
"""

from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from app.api.deps import Principal
from app.core.templating import templates
from app.i18n import translate
from app.web.api_client import internal_api_client
from app.web.csrf import attach_csrf_cookie, read_or_generate_csrf_token, verify_csrf
from app.web.deps import require_web_admin
from app.web.flash import redirect_with_flash

router = APIRouter(tags=["backoffice-email-templates"])

# Mirrors app.models.enums.EmailTemplateType.ORDER_CONFIRMATION_TICKET.value
# — the only type with a real send path today, see this module's docstring.
_TEMPLATE_TYPE = "order_confirmation_ticket"

# Mirrors app.i18n.SUPPORTED_LOCALES — kept here only as (value, label)
# pairs for the language-switcher tabs; the JSON API remains the single
# source of truth for which language codes are actually valid.
_LANGUAGES = [("en", "English"), ("nl", "Nederlands")]
_LANGUAGE_CODES = {code for code, _ in _LANGUAGES}

# Pre-fill values for a language/event combination that has no saved
# customization yet, so the editor and its initial preview never start on
# an empty/invalid template. Resolved through the same app.i18n.translate()
# lookup app.services.email_render.DEFAULT_SUBJECT/DEFAULT_BODY use (both
# read the same app/i18n/{locale}.json ``email.order_confirmation.*`` keys)
# — a direct import of translate() here, NOT a second literal copy of the
# copy itself, since app.i18n is a shared content-lookup module already
# imported directly by both the web and API layers elsewhere (e.g.
# app.web.routes.public_site), unlike app.services/app.models which this
# file deliberately never imports (see this module's docstring: everything
# else stays a thin proxy to the JSON API). This was previously a literal
# copy that could silently drift from the real default; content-i18n's
# Milestone 4 review folded both call sites onto one source.
def _default_subject(language: str) -> str:
    return translate("email.order_confirmation.subject", language)


def _default_body(language: str) -> str:
    return translate("email.order_confirmation.body", language)


# The real, fixed set of placeholder keys the send-time renderer actually
# substitutes — copied from app.services.email_render.build_placeholder_values
# (not guessed), shown to the admin/agent so they know exactly what's
# available. Keep this list in sync if that function's keys ever change.
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
    """Gather everything the editor page needs in one place: the event,
    its saved template for this type/language (if any), and an initial
    live-preview render using the saved (or built-in default) subject/body
    against the event's real theme."""
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
    preview_error = None if preview else _error_detail(preview_response, "Could not build preview.")

    return {
        "event": event,
        "template": template,
        "subject_value": subject_value,
        "body_value": body_value,
        "preview": preview,
        "preview_doc": preview["html_body"] if preview else None,
        "error": preview_error,
    }


def _error_detail(response: Any, fallback: str) -> str:
    """FastAPI/Pydantic validation failures (422) return ``detail`` as a
    list of error objects, not a string — fall back to a generic message
    rather than rendering a Python list repr in the page."""
    try:
        detail = response.json().get("detail", fallback)
    except Exception:  # noqa: BLE001 - defensive, response body may not be JSON at all
        return fallback
    return detail if isinstance(detail, str) else fallback


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
    """HTMX target: re-renders the live-preview pane from the current
    (unsaved) subject/body values against sample data and the event's real
    theme — called on every debounced field change, never on full submit."""
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
        detail = _error_detail(preview_response, "Enter a subject and body to preview.")
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
            _error_detail(put_response, "Could not save email template."),
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
    """Delete this event's customization for the language, reverting future
    sends (and this editor's own pre-fill) back to the built-in default."""
    verify_csrf(request, csrf_token)
    language = _normalize_language(language)
    async with internal_api_client(request) as client:
        resp = await client.delete(
            f"/api/v1/events/{event_id}/email-templates/{_TEMPLATE_TYPE}/{language}"
        )

    redirect_path = f"/events/{event_id}/email-templates?language={language}"
    if resp.status_code >= 400 and resp.status_code != 404:
        return redirect_with_flash(
            redirect_path, _error_detail(resp, "Could not reset email template."), kind="error"
        )
    return redirect_with_flash(
        redirect_path, "Reverted to the built-in default template.", kind="success"
    )

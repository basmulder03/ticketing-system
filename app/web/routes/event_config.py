"""Backoffice event settings: SMTP, Mollie, invoicing, sales timing and payment
methods, plus test-email, test-Mollie and copy-from-event actions.

Secrets are never echoed back, so their fields always render empty with an
"is set" indicator. A secret is sent on save only if a new value was typed,
or "clear" was ticked (sent as an empty string); otherwise it's omitted and
the saved value is left alone.
"""

from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from app.api.deps import Principal
from app.core.templating import templates
from app.web.api_client import api_error_detail, internal_api_client
from app.web.csrf import attach_csrf_cookie, read_or_generate_csrf_token, verify_csrf
from app.web.deps import require_web_admin
from app.web.flash import redirect_with_flash

router = APIRouter(tags=["backoffice-event-config"])

# Select options; the API enforces the real enums.
SMTP_ENCRYPTION_CHOICES = [
    ("none", "None (plain text — local dev only, e.g. Mailpit)"),
    ("starttls", "STARTTLS"),
    ("ssl", "Implicit TLS (SSL)"),
]

# Mirrors app.models.enums.MollieMode.
MOLLIE_MODE_CHOICES = [
    ("test", "Test"),
    ("live", "Live"),
]

# Mirrors app.models.enums.PaymentMethod.
PAYMENT_METHOD_CHOICES = [
    ("mollie", "Mollie (online payment)"),
    ("door", "Pay at the door"),
    ("demo", "Demo / test payment (no real charge)"),
]


def _secret_field_update(body: dict[str, Any], key: str, *, new_value: str, clear: bool) -> None:
    """Add one secret to the PUT body only if typed or explicitly cleared."""
    if clear:
        body[key] = ""
    elif new_value.strip():
        body[key] = new_value


async def _fetch_config_context(request: Request, event_id: str) -> dict[str, Any]:
    """The event, its config (``None`` if none yet), and other events to copy from."""
    async with internal_api_client(request) as client:
        event_response = await client.get(f"/api/v1/events/{event_id}")
        if event_response.status_code == 404:
            raise HTTPException(status_code=404, detail="Event not found.")
        event = event_response.json()

        config_response = await client.get(f"/api/v1/events/{event_id}/config")
        config = config_response.json() if config_response.status_code == 200 else None

        all_events_response = await client.get("/api/v1/events")
        other_events = [e for e in all_events_response.json() if e["id"] != event_id]

    return {"event": event, "config": config, "other_events": other_events}


@router.get("/events/{event_id}/config", response_model=None)
async def event_config_settings(
    request: Request, event_id: str, principal: Principal = Depends(require_web_admin)
) -> Response:
    ctx = await _fetch_config_context(request, event_id)
    token = read_or_generate_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "backoffice/event_config.html",
        {
            "principal": principal,
            "event": ctx["event"],
            "config": ctx["config"],
            "other_events": ctx["other_events"],
            "smtp_encryption_choices": SMTP_ENCRYPTION_CHOICES,
            "mollie_mode_choices": MOLLIE_MODE_CHOICES,
            "payment_method_choices": PAYMENT_METHOD_CHOICES,
            "csrf_token": token,
            "flash": request.query_params.get("flash"),
            "flash_kind": request.query_params.get("flash_kind", "success"),
        },
    )
    attach_csrf_cookie(response, token)
    return response


@router.post("/events/{event_id}/config")
async def save_event_config(
    request: Request,
    event_id: str,
    principal: Principal = Depends(require_web_admin),
    smtp_host: str = Form(""),
    smtp_port: str = Form(""),
    smtp_encryption: str = Form("none"),
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    clear_smtp_password: bool = Form(False),
    sender_name: str = Form(""),
    sender_email: str = Form(""),
    mollie_mode: str = Form("test"),
    mollie_test_api_key: str = Form(""),
    clear_mollie_test_api_key: bool = Form(False),
    mollie_live_api_key: str = Form(""),
    clear_mollie_live_api_key: bool = Form(False),
    invoice_company_name: str = Form(""),
    invoice_company_address: str = Form(""),
    invoice_company_vat_number: str = Form(""),
    invoice_number_prefix: str = Form(""),
    sales_live_at: str = Form(""),
    enabled_payment_methods: list[str] = Form([]),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    """Save the full form; secrets follow :func:`_secret_field_update`'s rule."""
    verify_csrf(request, csrf_token)
    redirect_path = f"/events/{event_id}/config"

    port_raw = smtp_port.strip()
    port_value: int | None
    if port_raw:
        try:
            port_value = int(port_raw)
        except ValueError:
            return redirect_with_flash(redirect_path, "SMTP port must be a whole number.", kind="error")
    else:
        port_value = None

    body: dict[str, Any] = {
        "smtp_host": smtp_host.strip() or None,
        "smtp_port": port_value,
        "smtp_encryption": smtp_encryption,
        "smtp_username": smtp_username.strip() or None,
        "sender_name": sender_name.strip() or None,
        "sender_email": sender_email.strip() or None,
        "mollie_mode": mollie_mode,
        "invoice_company_name": invoice_company_name.strip() or None,
        "invoice_company_address": invoice_company_address.strip() or None,
        "invoice_company_vat_number": invoice_company_vat_number.strip() or None,
        "invoice_number_prefix": invoice_number_prefix.strip() or None,
        # datetime-local has no timezone; the form says UTC, so append "Z".
        "sales_live_at": f"{sales_live_at.strip()}:00Z" if sales_live_at.strip() else None,
        "enabled_payment_methods": enabled_payment_methods,
    }
    _secret_field_update(body, "smtp_password", new_value=smtp_password, clear=clear_smtp_password)
    _secret_field_update(body, "mollie_test_api_key", new_value=mollie_test_api_key, clear=clear_mollie_test_api_key)
    _secret_field_update(body, "mollie_live_api_key", new_value=mollie_live_api_key, clear=clear_mollie_live_api_key)

    async with internal_api_client(request) as client:
        resp = await client.put(f"/api/v1/events/{event_id}/config", json=body)

    if resp.status_code >= 400:
        return redirect_with_flash(redirect_path, api_error_detail(resp, "Could not save configuration."), kind="error")
    return redirect_with_flash(redirect_path, "Configuration saved.", kind="success")


@router.post("/events/{event_id}/config/test-email")
async def test_email_web(
    request: Request,
    event_id: str,
    principal: Principal = Depends(require_web_admin),
    recipient: str = Form(...),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    """Send a test email using the *saved* SMTP settings — save first."""
    verify_csrf(request, csrf_token)
    redirect_path = f"/events/{event_id}/config"
    async with internal_api_client(request) as client:
        resp = await client.post(f"/api/v1/events/{event_id}/config/test-email", json={"recipient": recipient})

    if resp.status_code == 404:
        return redirect_with_flash(
            redirect_path, api_error_detail(resp, "Save SMTP settings before sending a test email."), kind="error"
        )
    if resp.status_code >= 400:
        return redirect_with_flash(redirect_path, api_error_detail(resp, "Could not send test email."), kind="error")

    result = resp.json()
    kind = "success" if result.get("success") else "error"
    return redirect_with_flash(redirect_path, result.get("message", "Test email attempted."), kind=kind)


@router.post("/events/{event_id}/config/test-mollie")
async def test_mollie_web(
    request: Request,
    event_id: str,
    principal: Principal = Depends(require_web_admin),
    environment: str = Form("test"),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    """Verify the *saved* Mollie key for the chosen environment — save first."""
    verify_csrf(request, csrf_token)
    redirect_path = f"/events/{event_id}/config"
    async with internal_api_client(request) as client:
        resp = await client.post(f"/api/v1/events/{event_id}/config/test-mollie", json={"environment": environment})

    if resp.status_code == 404:
        return redirect_with_flash(
            redirect_path, api_error_detail(resp, "Save a Mollie API key before testing the connection."), kind="error"
        )
    if resp.status_code >= 400:
        return redirect_with_flash(
            redirect_path, api_error_detail(resp, "Could not test the Mollie connection."), kind="error"
        )

    result = resp.json()
    kind = "success" if result.get("success") else "error"
    return redirect_with_flash(redirect_path, result.get("message", "Mollie connection tested."), kind=kind)


@router.post("/events/{event_id}/config/copy-from")
async def copy_event_config_web(
    request: Request,
    event_id: str,
    principal: Principal = Depends(require_web_admin),
    source_event_id: str = Form(...),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    """Copy another event's config onto this one (like copying a theme)."""
    verify_csrf(request, csrf_token)
    redirect_path = f"/events/{event_id}/config"
    async with internal_api_client(request) as client:
        resp = await client.post(f"/api/v1/events/{event_id}/config/copy-from/{source_event_id}")

    if resp.status_code >= 400:
        return redirect_with_flash(
            redirect_path, api_error_detail(resp, "Could not copy configuration."), kind="error"
        )
    return redirect_with_flash(redirect_path, "Configuration copied from the selected event.", kind="success")

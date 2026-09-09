"""Backoffice EventConfig settings page: SMTP, Mollie, invoice, sales-timing,
and payment-method configuration for one Event, plus "send test email",
"test Mollie connection", and "copy configuration from another event"
actions.

Every mutation is a thin proxy to the existing JSON API under
``app.api.routes.event_configs`` via ``app.web.api_client`` — secret
encryption-at-rest, connection testing, and audit logging all continue to
live exactly once in that module and the services it calls. This file only
translates between HTML form fields and that JSON API, and renders
templates.

**Secret handling (SMTP password, Mollie test/live API keys)**:
``EventConfigOut`` never echoes a decrypted secret back — only an
``*_is_set`` boolean (see ``app.schemas.event_config``) — so this page's
form fields for the three secrets always render empty, with a plain "a
secret is currently set" / "no secret set" text indicator next to each,
rather than any (unavailable) pre-filled value. On save
(:func:`save_event_config` below), a secret field is included in the
outgoing PUT body ONLY if the admin typed a new value into it, or
explicitly ticked its "clear this secret" checkbox (sent as an explicit
empty string, which the API treats as "clear it" per
``EventConfigUpdateRequest``'s own docstring) — left both blank and
unchecked, the field is omitted from the body entirely, which the API's
``exclude_unset`` semantics leave completely untouched rather than clearing
it. See :func:`_secret_field_update`.
"""

from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from app.api.deps import Principal
from app.core.templating import templates
from app.web.api_client import internal_api_client
from app.web.csrf import attach_csrf_cookie, read_or_generate_csrf_token, verify_csrf
from app.web.deps import require_web_admin
from app.web.flash import redirect_with_flash

router = APIRouter(tags=["backoffice-event-config"])

# Mirrors app.models.enums.SmtpEncryptionMode — local (value, label) pairs
# for the <select>, same convention as app.web.routes.themes.FONT_CHOICES;
# the JSON API enforces the real enum.
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
]


def _error_detail(response: Any, fallback: str) -> str:
    """Same convention as every other web-route module's copy of this
    helper — see ``app.web.routes.orders._error_detail``."""
    try:
        detail = response.json().get("detail", fallback)
    except Exception:  # noqa: BLE001 - response body may not be JSON at all
        return fallback
    return detail if isinstance(detail, str) else fallback


def _secret_field_update(body: dict[str, Any], key: str, *, new_value: str, clear: bool) -> None:
    """Apply one secret field's "leave untouched unless typed or explicitly
    cleared" write rule (see this module's docstring) to the outgoing PUT
    ``body``, mutating it in place. Adds nothing at all if the admin
    neither typed a replacement value nor ticked "clear this secret" — the
    field then stays entirely absent from the JSON body, which the API's
    ``exclude_unset`` semantics correctly read as "leave whatever is
    already saved alone"."""
    if clear:
        body[key] = ""
    elif new_value.strip():
        body[key] = new_value


async def _fetch_config_context(request: Request, event_id: str) -> dict[str, Any]:
    """Gather everything the settings page needs: the event, its config (or
    ``None`` if it has none yet — a PUT to the API creates one), and every
    other event (for the "copy configuration from" action) — same shape as
    ``app.web.routes.themes._fetch_editor_context``."""
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
    """Proxy to ``PUT /api/v1/events/{event_id}/config``
    (``app.api.routes.event_configs.upsert_event_config``).

    Every non-secret field is always included in the outgoing body — this
    is a full settings form, not a partial-diff UI, same convention as
    ``app.web.routes.themes.save_theme_fields`` — while the three secret
    fields follow the narrower "leave untouched unless typed or explicitly
    cleared" rule from this module's docstring (see
    :func:`_secret_field_update`).
    """
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
        # A bare <input type="datetime-local"> value carries no timezone of
        # its own; this field's label tells the admin it's UTC (see
        # backoffice/event_config.html), and an explicit "Z" suffix is
        # appended here so the API's datetime parsing is never ambiguous
        # about which offset was meant.
        "sales_live_at": f"{sales_live_at.strip()}:00Z" if sales_live_at.strip() else None,
        "enabled_payment_methods": enabled_payment_methods,
    }
    _secret_field_update(body, "smtp_password", new_value=smtp_password, clear=clear_smtp_password)
    _secret_field_update(body, "mollie_test_api_key", new_value=mollie_test_api_key, clear=clear_mollie_test_api_key)
    _secret_field_update(body, "mollie_live_api_key", new_value=mollie_live_api_key, clear=clear_mollie_live_api_key)

    async with internal_api_client(request) as client:
        resp = await client.put(f"/api/v1/events/{event_id}/config", json=body)

    if resp.status_code >= 400:
        return redirect_with_flash(redirect_path, _error_detail(resp, "Could not save configuration."), kind="error")
    return redirect_with_flash(redirect_path, "Configuration saved.", kind="success")


@router.post("/events/{event_id}/config/test-email")
async def test_email_web(
    request: Request,
    event_id: str,
    principal: Principal = Depends(require_web_admin),
    recipient: str = Form(...),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    """Proxy to ``POST /api/v1/events/{event_id}/config/test-email``
    (``app.api.routes.event_configs.test_email``) — sends a real test email
    using this event's currently SAVED SMTP settings, not whatever unsaved
    values happen to be sitting in the form on screen; save first if you
    just changed them.
    """
    verify_csrf(request, csrf_token)
    redirect_path = f"/events/{event_id}/config"
    async with internal_api_client(request) as client:
        resp = await client.post(f"/api/v1/events/{event_id}/config/test-email", json={"recipient": recipient})

    if resp.status_code == 404:
        return redirect_with_flash(
            redirect_path, _error_detail(resp, "Save SMTP settings before sending a test email."), kind="error"
        )
    if resp.status_code >= 400:
        return redirect_with_flash(redirect_path, _error_detail(resp, "Could not send test email."), kind="error")

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
    """Proxy to ``POST /api/v1/events/{event_id}/config/test-mollie``
    (``app.api.routes.event_configs.test_mollie``) — verifies this event's
    currently SAVED Mollie key for the chosen environment (test/live), not
    an unsaved value on screen; save first if you just changed it.
    """
    verify_csrf(request, csrf_token)
    redirect_path = f"/events/{event_id}/config"
    async with internal_api_client(request) as client:
        resp = await client.post(f"/api/v1/events/{event_id}/config/test-mollie", json={"environment": environment})

    if resp.status_code == 404:
        return redirect_with_flash(
            redirect_path, _error_detail(resp, "Save a Mollie API key before testing the connection."), kind="error"
        )
    if resp.status_code >= 400:
        return redirect_with_flash(
            redirect_path, _error_detail(resp, "Could not test the Mollie connection."), kind="error"
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
    """Proxy to ``POST /api/v1/events/{event_id}/config/copy-from/{source_event_id}``
    (``app.api.routes.event_configs.copy_event_config``) — mirrors
    ``app.web.routes.themes.copy_theme_from`` exactly, the identical
    existing "duplicate from another event" precedent for Theme.
    """
    verify_csrf(request, csrf_token)
    redirect_path = f"/events/{event_id}/config"
    async with internal_api_client(request) as client:
        resp = await client.post(f"/api/v1/events/{event_id}/config/copy-from/{source_event_id}")

    if resp.status_code >= 400:
        return redirect_with_flash(
            redirect_path, _error_detail(resp, "Could not copy configuration."), kind="error"
        )
    return redirect_with_flash(redirect_path, "Configuration copied from the selected event.", kind="success")

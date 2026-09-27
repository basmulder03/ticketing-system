"""Public HTML pages: event landing page (published and preview), checkout,
order confirmation, privacy policy, and the beamer countdown view.

Everything is proxied to the public JSON API; no business rules here. No
CSRF on checkout: there's no session cookie on the public site for a
cross-site request to ride on.
"""

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from app.core.config import get_settings
from app.core.public_templating import public_templates
from app.i18n import translate
from app.web.order_confirmation import read_order_confirmation, stash_order_confirmation
from app.web.public_api_client import public_api_client
from app.web.public_context import (
    LOCALE_COOKIE_NAME,
    build_beamer_theme_css,
    build_event_json_ld,
    build_public_theme_css,
    build_share_urls,
    resolve_locale,
)

router = APIRouter(tags=["public-site"])


def _set_locale_cookie(response: Response, locale: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=LOCALE_COOKIE_NAME,
        value=locale,
        max_age=60 * 60 * 24 * 365,
        httponly=False,  # the language-switcher UI reads/writes this via plain links, not JS
        samesite="lax",
        secure=settings.app_env != "development",
    )


async def _fetch_event(request: Request, *, slug: str | None, token: str | None) -> tuple[dict[str, Any] | None, int]:
    """The event by slug or preview token (exactly one), with the HTTP status."""
    path = f"/api/v1/public/events/{slug}" if slug is not None else f"/api/v1/public/preview/{token}"
    async with public_api_client(request) as client:
        response = await client.get(path)
    if response.status_code != 200:
        return None, response.status_code
    return response.json(), 200


def _not_found_response(request: Request, locale: str) -> Response:
    return public_templates.TemplateResponse(
        request,
        "public/not_found.html",
        {"locale": locale},
        status_code=404,
    )


def _find_show(event: dict[str, Any], show_id: str | None) -> dict[str, Any] | None:
    return next((s for s in event["shows"] if s["id"] == show_id), None)


def _default_selected_show_id(request: Request, event: dict[str, Any]) -> str | None:
    """Which show's panel opens first: ``?show=``, else the show of a deep-linked
    ``?ticket_type=``, else the first upcoming show.
    """
    requested_show = request.query_params.get("show")
    if requested_show and _find_show(event, requested_show):
        return requested_show

    requested_ticket_type = request.query_params.get("ticket_type")
    if requested_ticket_type:
        for show in event["shows"]:
            if any(tt["id"] == requested_ticket_type for tt in show["ticket_types"]):
                return str(show["id"])

    return event["shows"][0]["id"] if event["shows"] else None


def _default_beamer_show_id(event: dict[str, Any]) -> str | None:
    """Which show the beamer counts down to without ``?show=``: the soonest whose
    doors haven't opened, else the most recent one, so an unattended screen
    always shows something. ``None`` only if there are no shows.

    Uses naive local time, like ``Show.date``/``doors_time`` (the app assumes
    venue and server share a timezone).
    """
    if not event["shows"]:
        return None

    def doors_at(show: dict[str, Any]) -> datetime:
        return datetime.fromisoformat(f"{show['date']}T{show['doors_time']}")

    # Naive local time: show dates/times carry no timezone.
    now = datetime.now()  # noqa: DTZ005
    upcoming = sorted((s for s in event["shows"] if doors_at(s) >= now), key=doors_at)
    if upcoming:
        return str(upcoming[0]["id"])
    return str(max(event["shows"], key=doors_at)["id"])


def _sellable_shows(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Shows with at least one ticket type — the rest would be a dead end in the
    picker. Filtered here, not in the API, because the beamer view still wants
    them.
    """
    return [show for show in event["shows"] if show["ticket_types"]]


def _render_landing(
    request: Request,
    event: dict[str, Any],
    *,
    is_preview: bool,
    preview_token: str | None,
    locale: str,
    sticky: dict[str, Any] | None = None,
    checkout_error: str | None = None,
    status_code: int = 200,
) -> Response:
    # The public page offers only sellable shows; preview keeps them all so the
    # organizer sees shows still missing ticket types.
    if not is_preview:
        event = {**event, "shows": _sellable_shows(event)}

    settings = get_settings()
    base_url = settings.public_base_url.rstrip("/")

    if is_preview:
        page_url = f"{base_url}/preview/{preview_token}"
        canonical_url = None  # a draft/preview page is never indexed, so it gets no canonical URL either
        checkout_action = f"/preview/{preview_token}/checkout"
    else:
        page_url = f"{base_url}/e/{event['slug']}"
        canonical_url = page_url
        checkout_action = f"/e/{event['slug']}/checkout"

    theme = event.get("theme")
    og_image = None
    if theme:
        og_image = theme.get("background_image_url") or theme.get("logo_url")
        if og_image and og_image.startswith("/"):
            og_image = f"{base_url}{og_image}"

    meta_description = event.get("description") or (
        f"{translate('public.seo.default_description_prefix', locale)} {event['name']}"
    )

    selected_show_id = (sticky or {}).get("show_choice") or _default_selected_show_id(request, event)
    highlight_ticket_type_id = request.query_params.get("ticket_type")

    sales_live_at_raw = event.get("sales_live_at")
    sales_live_now = True
    if sales_live_at_raw:
        sales_live_now = datetime.now(UTC) >= datetime.fromisoformat(sales_live_at_raw)

    context = {
        "event": event,
        "locale": locale,
        "is_preview": is_preview,
        "canonical_url": canonical_url,
        "page_url": page_url,
        "meta_description": meta_description,
        "og_image": og_image,
        "json_ld": build_event_json_ld(event, page_url),
        "theme_css": build_public_theme_css(theme),
        "checkout_action": checkout_action,
        "selected_show_id": selected_show_id,
        "highlight_ticket_type_id": highlight_ticket_type_id,
        "sales_live_now": sales_live_now,
        "sales_live_at_raw": sales_live_at_raw,
        "share_urls": build_share_urls(
            page_url=page_url,
            share_text=f"{translate('public.share.text_prefix', locale)} {event['name']}",
        ),
        "checkout_error": checkout_error,
        "sticky": sticky or {},
        "home_url": page_url,
    }
    return public_templates.TemplateResponse(
        request, "public/landing.html", context, status_code=status_code
    )


@router.get("/e/{slug}", response_model=None)
async def public_landing_page(request: Request, slug: str) -> Response:
    """A published event's page; drafts and unknown slugs 404 identically."""
    locale = resolve_locale(request)
    event, _fetch_status = await _fetch_event(request, slug=slug, token=None)
    response = _not_found_response(request, locale) if event is None else _render_landing(
        request, event, is_preview=False, preview_token=None, locale=locale
    )
    _set_locale_cookie(response, locale)
    return response


@router.get("/preview/{token}", response_model=None)
async def preview_landing_page(request: Request, token: str) -> Response:
    """The preview-token version: identical, plus ``noindex``."""
    locale = resolve_locale(request)
    event, _fetch_status = await _fetch_event(request, slug=None, token=token)
    response = _not_found_response(request, locale) if event is None else _render_landing(
        request, event, is_preview=True, preview_token=token, locale=locale
    )
    _set_locale_cookie(response, locale)
    return response


def _render_beamer(request: Request, event: dict[str, Any], show: dict[str, Any], *, locale: str) -> Response:
    """Render the beamer view for one show: name, venue, doors time for the
    countdown (and whether it's passed, for no-JS), and beamer-safe theme CSS.
    """
    theme = event.get("theme")
    doors_at_raw = f"{show['date']}T{show['doors_time']}"
    # Deliberately naive/local, same rationale as _default_beamer_show_id.
    doors_passed = datetime.fromisoformat(doors_at_raw) <= datetime.now()  # noqa: DTZ005

    context = {
        "event": event,
        "show": show,
        "locale": locale,
        "venue_name": show["venue_name"],
        "doors_at_raw": doors_at_raw,
        "doors_passed": doors_passed,
        "theme_css": build_beamer_theme_css(theme),
        "logo_url": theme.get("logo_url") if theme else None,
    }
    return public_templates.TemplateResponse(request, "beamer/show.html", context)


async def _handle_beamer_page(request: Request, *, slug: str | None, token: str | None) -> Response:
    """Shared beamer handler (slug or token): resolve the event and show
    (``?show=`` or the default), 404 if either is missing.
    """
    locale = resolve_locale(request)
    event, _fetch_status = await _fetch_event(request, slug=slug, token=token)

    show = None
    if event is not None:
        requested_show_id = request.query_params.get("show")
        show = _find_show(event, requested_show_id) if requested_show_id else None
        if show is None:
            show = _find_show(event, _default_beamer_show_id(event))

    if event is None or show is None:
        response = _not_found_response(request, locale)
    else:
        response = _render_beamer(request, event, show, locale=locale)
    _set_locale_cookie(response, locale)
    return response


@router.get("/e/{slug}/beamer", response_model=None)
async def public_beamer_page(request: Request, slug: str) -> Response:
    """Full-screen countdown to one show's doors time, for a lobby screen or TV.

    Pick a show with ``?show=<id>``; otherwise the soonest upcoming one. 404s
    (like the landing page) for drafts, unknown slugs, no shows, or an unknown
    show id.
    """
    return await _handle_beamer_page(request, slug=slug, token=None)


@router.get("/preview/{token}/beamer", response_model=None)
async def preview_beamer_page(request: Request, token: str) -> Response:
    """Preview-token beamer view; draft shows are eligible too."""
    return await _handle_beamer_page(request, slug=None, token=token)


def _translate_checkout_error(status_code: int, detail: str, locale: str) -> str:
    """Turn a checkout error into a specific translated message (sold out, paused
    and not-yet-live must stay distinct). Matches on status and English
    ``detail`` text because the API has no error codes — brittle; an
    ``error_code`` field would be better.
    """
    if status_code == 404:
        return translate("public.checkout.error_unavailable", locale)
    if status_code == 403 and "paused" in detail:
        return translate("public.checkout.error_sales_paused", locale)
    if status_code == 403 and "not live" in detail:
        return translate("public.checkout.error_sales_not_live", locale)
    if status_code == 409:
        return translate("public.checkout.error_sold_out", locale)
    if status_code == 422 and "payment method" in detail.lower():
        return translate("public.checkout.error_payment_method_not_enabled", locale)
    return translate("public.checkout.error_generic", locale)


def _parse_checkout_items(
    event: dict[str, Any], form: dict[str, str], selected_show_id: str | None
) -> list[dict[str, Any]]:
    """Quantities for the selected show only. Without JS every panel's fields are
    submitted, so others are ignored here.
    """
    show = _find_show(event, selected_show_id)
    if show is None:
        return []
    items = []
    for ticket_type in show["ticket_types"]:
        raw_qty = form.get(f"qty_{ticket_type['id']}", "0")
        try:
            qty = int(raw_qty)
        except ValueError:
            qty = 0
        if qty > 0:
            items.append({"ticket_type_id": ticket_type["id"], "quantity": qty})
    return items


async def _handle_checkout_submission(
    request: Request, *, slug: str | None, token: str | None
) -> Response:
    locale = resolve_locale(request)
    event, _fetch_status = await _fetch_event(request, slug=slug, token=token)
    if event is None:
        return _not_found_response(request, locale)

    form = await request.form()
    form_data = {key: str(value) for key, value in form.multi_items()}
    selected_show_id = form_data.get("show_choice")
    sticky = dict(form_data)

    items = _parse_checkout_items(event, form_data, selected_show_id)
    if not items:
        return _render_landing(
            request,
            event,
            is_preview=token is not None,
            preview_token=token,
            locale=locale,
            sticky=sticky,
            checkout_error=translate("public.checkout.error_no_items", locale),
            status_code=422,
        )

    body: dict[str, Any] = {
        "buyer_name": form_data.get("buyer_name", ""),
        "buyer_email": form_data.get("buyer_email", ""),
        "buyer_address": form_data.get("buyer_address", ""),
        "language": locale,
        "payment_method": form_data.get("payment_method", ""),
        "items": items,
    }
    if token is not None:
        body["preview_token"] = token

    async with public_api_client(request) as client:
        checkout_response = await client.post("/api/v1/public/checkout", json=body)

    if checkout_response.status_code >= 400:
        try:
            raw_detail = checkout_response.json().get("detail", "")
        except ValueError:
            raw_detail = ""
        # A Pydantic 422 has a list ``detail``, not a string; normalize it so
        # _translate_checkout_error doesn't crash.
        detail = raw_detail if isinstance(raw_detail, str) else ""
        return _render_landing(
            request,
            event,
            is_preview=token is not None,
            preview_token=token,
            locale=locale,
            sticky=sticky,
            checkout_error=_translate_checkout_error(checkout_response.status_code, detail, locale),
            status_code=checkout_response.status_code,
        )

    order = checkout_response.json()
    payment_redirect_url = order.get("payment_redirect_url")
    if payment_redirect_url:
        # Send the buyer to the payment page (Mollie or demo). Both end at
        # /order-confirmation/<id>, which reads the cookie set on this response.
        response = RedirectResponse(url=payment_redirect_url, status_code=303)
    else:
        # Door orders and sandbox-paid orders go straight to confirmation.
        redirect_url = f"/order-confirmation/{order['id']}"
        if token is not None:
            redirect_url += "?" + urlencode({"preview": "1"})
        response = RedirectResponse(url=redirect_url, status_code=303)
    stash_order_confirmation(
        response,
        {
            "id": order["id"],
            "event_name": event["name"],
            "event_slug": event.get("slug"),
            "is_preview": token is not None,
            "buyer_name": order["buyer_name"],
            "buyer_email": order["buyer_email"],
            "buyer_address": order["buyer_address"],
            "payment_method": order["payment_method"],
            "status": order["status"],
            "total": str(order["total"]),
            "language": order["language"],
            "tickets": order["tickets"],
        },
    )
    _set_locale_cookie(response, locale)
    return response


@router.post("/e/{slug}/checkout", response_model=None)
async def submit_checkout(request: Request, slug: str) -> Response:
    return await _handle_checkout_submission(request, slug=slug, token=None)


@router.post("/preview/{token}/checkout", response_model=None)
async def submit_preview_checkout(request: Request, token: str) -> Response:
    return await _handle_checkout_submission(request, slug=None, token=token)


@router.get("/privacy", response_model=None)
async def privacy_policy(request: Request) -> Response:
    """The privacy policy page (placeholder content, not themed)."""
    locale = resolve_locale(request)
    return public_templates.TemplateResponse(
        request,
        "public/privacy_policy.html",
        {"locale": locale},
    )


@router.get("/order-confirmation/{order_id}", response_model=None)
async def order_confirmation(request: Request, order_id: str) -> Response:
    """The confirmation page; renders only in the buyer's own browser (see
    ``app.web.order_confirmation``).
    """
    locale = resolve_locale(request)
    order = read_order_confirmation(request, order_id)
    if order is None:
        return public_templates.TemplateResponse(
            request,
            "public/order_confirmation_unavailable.html",
            {"locale": locale},
            status_code=404,
        )
    context = {
        "locale": locale,
        "order": order,
        "is_preview": bool(request.query_params.get("preview")) or order.get("is_preview", False),
        "now": datetime.now(UTC).isoformat(),
        "home_url": f"/e/{order['event_slug']}" if order.get("event_slug") else None,
    }
    response = public_templates.TemplateResponse(request, "public/order_confirmation.html", context)
    _set_locale_cookie(response, locale)
    return response

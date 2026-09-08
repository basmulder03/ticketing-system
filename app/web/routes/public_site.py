"""Public-site HTML pages (Milestone 2): the themed event landing page
(published-slug and unguessable-preview-token variants), the checkout form
submission, and the order confirmation page.

Every route here proxies in-process to the existing public JSON API
(``app.api.routes.public``) via ``app.web.public_api_client`` — no
business logic (draft/publish gating, stock checks, sales-timing rules)
is duplicated here; this module only translates between HTML
forms/templates and that JSON API. Deliberately unauthenticated
throughout (this is the public buyer-facing site) and deliberately
CSRF-free on the checkout form: there is no session cookie here for a
cross-site request to ride on (see ``app.web.csrf`` for the backoffice's
double-submit-cookie pattern, which exists specifically because *that*
surface has an authenticated session to protect).
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
    """Fetch an Event via the published-slug or preview-token public API
    route (exactly one of ``slug``/``token`` should be given). Returns
    ``(event_or_none, http_status)``."""
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
    """Which Show's ticket-type panel should be open by default: an
    explicit ``?show=`` query param, else the Show containing a deep-linked
    ``?ticket_type=`` (per PROJECT_BRIEF.md's Sharing section: "a specific
    ticket type (deep link that pre-selects it in the picker)"), else the
    first upcoming Show, else nothing (no shows at all)."""
    requested_show = request.query_params.get("show")
    if requested_show and _find_show(event, requested_show):
        return requested_show

    requested_ticket_type = request.query_params.get("ticket_type")
    if requested_ticket_type:
        for show in event["shows"]:
            if any(tt["id"] == requested_ticket_type for tt in show["ticket_types"]):
                return str(show["id"])

    return event["shows"][0]["id"] if event["shows"] else None


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
    """The published Event landing page — 404s (with the same response
    shape the underlying API gives) for a draft Event or a slug that
    doesn't exist, so a URL guess can't distinguish the two."""
    locale = resolve_locale(request)
    event, _fetch_status = await _fetch_event(request, slug=slug, token=None)
    response = _not_found_response(request, locale) if event is None else _render_landing(
        request, event, is_preview=False, preview_token=None, locale=locale
    )
    _set_locale_cookie(response, locale)
    return response


@router.get("/preview/{token}", response_model=None)
async def preview_landing_page(request: Request, token: str) -> Response:
    """The unguessable-preview-token landing page for a draft (or already-
    published) Event — identical rendering to the real page (per
    PROJECT_BRIEF.md's Draft & Preview section: "should look identical to
    the real page otherwise"), plus a noindex meta tag (see
    ``app/templates/public/base.html``)."""
    locale = resolve_locale(request)
    event, _fetch_status = await _fetch_event(request, slug=None, token=token)
    response = _not_found_response(request, locale) if event is None else _render_landing(
        request, event, is_preview=True, preview_token=token, locale=locale
    )
    _set_locale_cookie(response, locale)
    return response


def _translate_checkout_error(status_code: int, detail: str, locale: str) -> str:
    """Map a ``CheckoutError`` HTTP response to a specific, translated
    buyer-facing message — per this milestone's brief: "sold out vs. sales
    paused vs. sales not live yet are different situations, don't collapse
    them into one generic error". The JSON API (``app.services.checkout``)
    doesn't expose a stable machine-readable error code today, only an
    HTTP status plus a fixed English ``detail`` string, so this matches on
    that status/text combination rather than duplicating the check's logic
    — flagged for `backend-builder` in this handoff as a good candidate for
    a proper ``error_code`` field in a later milestone, since text-matching
    a message that was never contracted to stay stable is inherently
    brittle.
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
    """Read ``qty_<ticket_type_id>`` fields for the Show the buyer selected
    only — any quantities submitted for a Show the buyer did NOT select are
    ignored (see ``app/templates/public/landing.html``'s per-show panel
    markup: only one panel is meant to be active at a time, but a
    JS-disabled browser would still submit every panel's fields, so this is
    enforced here rather than trusted from the client)."""
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
        # A CheckoutError's detail is always a string, but a 422 from
        # Pydantic's OWN request-body validation (e.g. a required field
        # missing entirely — reachable by a JS-disabled/non-browser client
        # bypassing HTML5 `required`, not just a CheckoutError) instead
        # returns a list of error-object dicts. _translate_checkout_error
        # assumes a string (it calls .lower() on it), so without this
        # normalization that shape crashes the request with a 500 instead
        # of showing an accessible error message — found by the
        # accessibility test suite exercising exactly this path.
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


@router.get("/order-confirmation/{order_id}", response_model=None)
async def order_confirmation(request: Request, order_id: str) -> Response:
    """The post-checkout confirmation page. Only ever renders real order
    data in the buyer's own browser, immediately after their own checkout
    — see ``app.web.order_confirmation`` for why (and why this page is
    structurally not shareable, per PROJECT_BRIEF.md's Sharing section).
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

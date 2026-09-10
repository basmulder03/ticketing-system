"""Public-site HTML pages (Milestone 2): the themed event landing page
(published-slug and unguessable-preview-token variants), the checkout form
submission, and the order confirmation page. Milestone 3 adds: redirecting
the buyer to Mollie's hosted checkout when the JSON checkout API just
created a real Mollie payment for their order (see
``_handle_checkout_submission`` below). Milestone 9 adds: the large-display
"beamer/TV" countdown view (``public_beamer_page``/``preview_beamer_page``),
a dedicated chrome-free page for one Show, reusing the same slug/token
resolution as the landing page.

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


def _default_beamer_show_id(event: dict[str, Any]) -> str | None:
    """Which Show the large-display "beamer" view (``_render_beamer``)
    defaults to when its caller gives no explicit ``?show=`` query param:
    the soonest Show whose doors time hasn't passed yet, or — once every
    Show under this Event has already opened its doors — the most
    recently-started one, so an unattended screen still has something
    sensible to display instead of the page erroring out the moment the
    last performance's doors open. Returns ``None`` only when the Event
    has no Shows at all.

    Deliberately time-based ("next upcoming"), unlike
    ``_default_selected_show_id``'s "first Show in the list" fallback for
    the landing page's ticket picker — the landing page always shows every
    Show as an explicit choice for the buyer to pick from, so its default
    only decides which panel opens first; the beamer view shows exactly
    one Show with no picker at all, so its default has to pick the Show a
    lobby screen would actually want counting down to *right now*.

    Compares against naive local time to match ``Show.date``/
    ``doors_time``'s own naive-local-time storage (see
    ``app.models.show.Show``'s docstring: this app assumes every venue is
    in the same timezone, so there is no tz-aware datetime to compare
    against here) — the same assumption every other reader of these two
    fields already makes, e.g. ``app/templates/public/landing.html``'s
    ``format_time``/``format_date`` filters.
    """
    if not event["shows"]:
        return None

    def doors_at(show: dict[str, Any]) -> datetime:
        return datetime.fromisoformat(f"{show['date']}T{show['doors_time']}")

    # Deliberately naive/local (see docstring above) -- Show.date/doors_time
    # have no tz of their own to compare against, unlike every other
    # datetime in this codebase (which is why this needs a noqa here and
    # nowhere else).
    now = datetime.now()  # noqa: DTZ005
    upcoming = sorted((s for s in event["shows"] if doors_at(s) >= now), key=doors_at)
    if upcoming:
        return str(upcoming[0]["id"])
    return str(max(event["shows"], key=doors_at)["id"])


def _sellable_shows(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Shows a buyer could actually select on the landing page's ticket
    picker -- a Show with zero TicketType rows has nothing to sell yet, so
    listing it as a choosable date leads to a dead end (its panel would
    just be an empty ticket-type table with no way to buy anything; found
    via the user's own manual testing).

    Deliberately a presentation-layer filter here, not something
    ``app.api.routes.public``'s shared nested-event response does itself —
    that same response also backs the beamer/TV countdown view, which has
    no ticket picker and is a completely valid thing to point at a Show
    still awaiting its ticket types (see that module's own docstring)."""
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
    # The real, buyer-facing landing page only ever offers a sellable Show
    # as a choice; preview mode deliberately keeps every Show (including
    # one still missing its ticket types) so an organizer reviewing a
    # draft sees that gap instead of it silently vanishing from what
    # they're checking. Every use of `event["shows"]` below (the picker,
    # the default-show-selection helpers, JSON-LD) sees this same filtered
    # list once it's swapped in here.
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


def _render_beamer(request: Request, event: dict[str, Any], show: dict[str, Any], *, locale: str) -> Response:
    """Render the large-display "beamer" view (Milestone 9 — see
    PROJECT_BRIEF.md's Responsive & Multi-Device section) for one
    already-resolved ``show`` under ``event``.

    Deliberately minimal compared to ``_render_landing``: this page has no
    checkout/share/SEO/JSON-LD context at all, so only the handful of
    values ``app/templates/beamer/show.html`` actually renders are built
    here — the Event's name, this Show's venue name, an ISO
    ``date``+``doors_time`` string for the client-side countdown script to
    parse, whether that deadline has already passed (so the initial
    server-rendered state is correct even with JS disabled, same
    progressive-enhancement principle as ``_render_landing``'s
    ``sales_live_now``), and the Theme's beamer-safe CSS (see
    ``build_beamer_theme_css`` for why this is NOT the same ``theme_css``
    ``_render_landing`` builds).
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
    """Shared implementation behind ``public_beamer_page``/
    ``preview_beamer_page`` (exactly one of ``slug``/``token`` is given) —
    mirrors ``_handle_checkout_submission``'s same slug-vs-token sharing
    pattern in this module. Resolves the Event, then the Show to display
    (an explicit ``?show=`` query param if it names a real Show under this
    Event, else ``_default_beamer_show_id``'s "next upcoming" choice), and
    404s (same shape/response as the landing page's own 404) if either
    lookup comes up empty — including an Event with zero Shows at all,
    since there is nothing for this view to count down to.
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
    """The large-display ("beamer/TV") countdown view for a published
    Event — a dedicated, chrome-free, full-screen page meant to be
    projected in a venue lobby (or left open unattended on a TV), showing
    a live countdown to one Show's doors time, the Event name, and that
    Show's venue name (see ``app/templates/beamer/show.html``). Per
    PROJECT_BRIEF.md's Responsive & Multi-Device section: "reachable via
    its own URL so it can be left open on a screen unattended".

    Which Show it counts down to: an explicit ``?show=<id>`` query param
    naming a Show under this Event, else the soonest upcoming Show (see
    ``_default_beamer_show_id``). There is deliberately no separate
    show-picker page for this view (unlike ``/scan/pick`` in
    ``app.web.routes.scan``): the intended usage is one screen, left open,
    pointed at one specific performance — an operator wanting a different
    Show for the same Event just appends ``?show=`` to this same URL.

    404s (same response shape as ``public_landing_page``) for a draft
    Event, an unknown slug, an Event with no Shows, or a ``?show=`` id
    that isn't a (published) Show under this Event — so a URL/query guess
    can't distinguish those reasons, same rationale as the landing page.
    """
    return await _handle_beamer_page(request, slug=slug, token=None)


@router.get("/preview/{token}/beamer", response_model=None)
async def preview_beamer_page(request: Request, token: str) -> Response:
    """The unguessable-preview-token variant of ``public_beamer_page`` —
    reachable via an Event's ``preview_token`` (see
    ``app.models.event.Event.preview_token``) the same way
    ``preview_landing_page`` is, so a stakeholder testing a draft (or
    already-published) Event before launch can also test-drive its beamer
    view ahead of time. Every Show under the Event is eligible here
    (draft or published — matching ``preview_landing_page``'s own
    ``published_only=False`` behavior), not just published ones.
    """
    return await _handle_beamer_page(request, slug=None, token=token)


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
    payment_redirect_url = order.get("payment_redirect_url")
    if payment_redirect_url:
        # Milestone 3 / post-launch fix: a real Mollie payment, or a `demo`
        # order's in-app demo-payment page, was just created for this order
        # — send the buyer there instead of straight to order-confirmation.
        # Mollie redirects back to the `redirectUrl` this app itself
        # supplied when creating the payment (see
        # `app.services.checkout._initiate_mollie_payment`), and the
        # demo-payment page's own "simulate success/failure" actions
        # redirect onward themselves (see
        # `app.web.routes.demo_payment`) — both eventually land back at
        # `/order-confirmation/{id}` (with `?preview=1` when relevant), so
        # the stashed cookie below (set now, on THIS response, before any
        # of that redirect chain even starts) is still what renders that
        # page once the buyer gets there, same as every other payment
        # method.
        response = RedirectResponse(url=payment_redirect_url, status_code=303)
    else:
        # `door` orders, and the preview-mode simulated-payment path (order
        # already `paid` with no real Mollie payment involved — see
        # `app.services.checkout`), both go straight to order-confirmation.
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
    """The public privacy policy page (Milestone 9's GDPR-conscious
    requirement — see PROJECT_BRIEF.md's Security & Ops section).

    Deliberately NOT Event-scoped/themed (unlike ``public_landing_page``):
    a privacy policy describes this whole deployment's data handling, not
    any one Event, so it renders through the same shared public template
    environment (``app.core.public_templating``) with no ``theme_css``/
    per-Event context — the same "plain page" shape as
    ``public/not_found.html``. Its actual content is placeholder text (see
    ``app/templates/public/privacy_policy.html``); this route only wires
    it up, it makes no claim about the content's legal accuracy.
    """
    locale = resolve_locale(request)
    return public_templates.TemplateResponse(
        request,
        "public/privacy_policy.html",
        {"locale": locale},
    )


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

"""HTML pages for the ``demo`` payment provider's interstitial (post-launch
fix, per the user's NOTES: a real, selectable payment method any event can
enable for demo/test purposes, without external calls or credentials — see
``app.services.checkout._initiate_demo_payment`` for the full design
rationale).

Same proxy-in-process-to-the-JSON-API posture as
``app.web.routes.public_site`` (via ``app.web.public_api_client``): no
business logic (order lookup, eligibility gating, settlement) is
duplicated here, this module only translates between the JSON API
(``app.api.routes.public``'s ``/demo-payment/{order_id}`` routes) and
HTML. Deliberately unauthenticated, like the rest of the public site — see
``app.api.routes.public._get_pending_demo_order_or_404`` for why an
order's own UUID is sufficient "possession" proof for this demo-only,
no-real-money feature.
"""

from fastapi import APIRouter, Request
from httpx import AsyncClient
from starlette.responses import RedirectResponse, Response

from app.core.public_templating import public_templates
from app.i18n import translate
from app.web.public_api_client import public_api_client
from app.web.public_context import resolve_locale

router = APIRouter(tags=["demo-payment"])


async def _unavailable_page(request: Request, locale: str) -> Response:
    return public_templates.TemplateResponse(
        request,
        "public/demo_payment_unavailable.html",
        {"locale": locale},
        status_code=404,
    )


async def _demo_payment_page_with_error(
    request: Request, client: AsyncClient, order_id: str, locale: str
) -> Response:
    """Re-render the interstitial itself with a generic error notice — for
    anything that ISN'T a 404 (order gone/already settled). The JSON
    API's complete/fail actions share ``checkout_rate_limiter`` with the
    checkout endpoint itself (a deliberate minimal-scope reuse — see
    ``app.web.routes.demo_payment`` module docstring), so a 429 here is a
    real, expected outcome, not a sign the order is gone; collapsing it
    into the "unavailable" page would misleadingly tell a rate-limited
    buyer their order was lost. Mirrors ``app.web.routes.public_site
    ._handle_checkout_submission``'s own "re-render the same page with a
    translated error" precedent for its checkout errors."""
    read_response = await client.get(f"/api/v1/public/demo-payment/{order_id}")
    if read_response.status_code >= 400:
        return await _unavailable_page(request, locale)
    return public_templates.TemplateResponse(
        request,
        "public/demo_payment.html",
        {
            "locale": locale,
            "order": read_response.json(),
            "error": translate("public.demo_payment.error_generic", locale),
        },
        status_code=502,
    )


@router.get("/demo-payment/{order_id}", response_model=None)
async def demo_payment_page(request: Request, order_id: str) -> Response:
    """Render the demo-payment interstitial, or a 404 page if this
    order_id isn't (or is no longer) an eligible ``demo``+``pending``
    order — same "can't distinguish doesn't-exist from already-settled"
    posture the read endpoint itself uses."""
    locale = resolve_locale(request)
    async with public_api_client(request) as client:
        response = await client.get(f"/api/v1/public/demo-payment/{order_id}")

    if response.status_code >= 400:
        return await _unavailable_page(request, locale)

    return public_templates.TemplateResponse(
        request,
        "public/demo_payment.html",
        {"locale": locale, "order": response.json()},
    )


@router.post("/demo-payment/{order_id}/complete", response_model=None)
async def complete_demo_payment_page(request: Request, order_id: str) -> Response:
    """The buyer's "simulate successful payment" form submission — settles
    the order via the JSON API, then redirects to the same
    ``/order-confirmation/{id}`` every other payment method lands on (the
    confirmation cookie was already stashed at checkout time, before this
    page was even reached — see ``app.web.routes.public_site
    ._handle_checkout_submission``'s comment on that)."""
    locale = resolve_locale(request)
    async with public_api_client(request) as client:
        api_response = await client.post(f"/api/v1/public/demo-payment/{order_id}/complete")

        if api_response.status_code == 404:
            return await _unavailable_page(request, locale)
        if api_response.status_code >= 400:
            return await _demo_payment_page_with_error(request, client, order_id, locale)

    return RedirectResponse(url=f"/order-confirmation/{order_id}", status_code=303)


@router.post("/demo-payment/{order_id}/fail", response_model=None)
async def fail_demo_payment_page(request: Request, order_id: str) -> Response:
    """The buyer's "simulate failed payment" form submission — cancels the
    order via the JSON API, then redirects to the same order-confirmation
    page a real Mollie cancellation/failure would (it renders whatever
    status the stashed cookie carries; there is no dedicated "your demo
    payment failed" page, mirroring how a real declined Mollie payment
    also just lands the buyer back on their own order-confirmation page,
    not a special one)."""
    locale = resolve_locale(request)
    async with public_api_client(request) as client:
        api_response = await client.post(f"/api/v1/public/demo-payment/{order_id}/fail")

        if api_response.status_code == 404:
            return await _unavailable_page(request, locale)
        if api_response.status_code >= 400:
            return await _demo_payment_page_with_error(request, client, order_id, locale)

    return RedirectResponse(url=f"/order-confirmation/{order_id}", status_code=303)

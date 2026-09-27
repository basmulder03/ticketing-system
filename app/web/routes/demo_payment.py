"""HTML pages for the demo payment provider. Unauthenticated like the rest of
the public site: the order's UUID is proof enough for a no-real-money demo.
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
    """Re-render the demo page with a retry notice, for any error except 404.
    The actions share the checkout rate limiter, so a 429 is normal and must
    not be shown as "your order is gone".
    """
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
    """Render the demo page, or the "unavailable" page if the order isn't a pending demo order."""
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
    """"Simulate success": settle via the API, then go to order confirmation
    (its cookie was set at checkout).
    """
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
    """"Simulate failure": cancel via the API, then go to order confirmation —
    like a declined real payment.
    """
    locale = resolve_locale(request)
    async with public_api_client(request) as client:
        api_response = await client.post(f"/api/v1/public/demo-payment/{order_id}/fail")

        if api_response.status_code == 404:
            return await _unavailable_page(request, locale)
        if api_response.status_code >= 400:
            return await _demo_payment_page_with_error(request, client, order_id, locale)

    return RedirectResponse(url=f"/order-confirmation/{order_id}", status_code=303)

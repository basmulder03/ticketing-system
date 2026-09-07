"""SEO infrastructure: ``/sitemap.xml`` and ``/robots.txt``, listing only
published, non-preview Event URLs — per PROJECT_BRIEF.md's Draft & Preview
section, a draft/preview page must never be indexed or listed.

Mounted at the application root (no ``/api/v1`` prefix) since both paths
are conventionally expected at the site root by crawlers.
"""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import PlainTextResponse, Response

from app.core.config import get_settings
from app.db.session import get_session
from app.models.enums import PublishStatus
from app.models.event import Event

router = APIRouter(tags=["seo"])


@router.get("/sitemap.xml")
async def sitemap(session: AsyncSession = Depends(get_session)) -> Response:
    """List every published Event's public landing-page URL.

    The public landing page lives at ``/e/{slug}`` (see
    ``app.web.routes.public_site.public_landing_page``) — this was
    originally written assuming ``/events/{slug}`` before that route
    existed; updated by `frontend-theming` to match the real scheme once
    the public pages were built (see that module's own docstring for why
    ``/e/`` was chosen: short, semantic, not query-string routing).
    """
    settings = get_settings()
    result = await session.execute(select(Event.slug).where(Event.status == PublishStatus.PUBLISHED))
    slugs = result.scalars().all()
    base_url = settings.public_base_url.rstrip("/")
    urls = "".join(f"<url><loc>{base_url}/e/{slug}</loc></url>" for slug in slugs)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + urls + "</urlset>"
    )
    return Response(content=xml, media_type="application/xml")


@router.get("/robots.txt")
async def robots_txt() -> Response:
    """Allow crawling of the public site, but explicitly disallow the
    unguessable ``/preview/`` paths (defense in depth on top of them simply
    never being linked/listed anywhere) and the buyer-private
    ``/order-confirmation/`` pages (never meant to be indexed — see
    ``app.web.routes.public_site.order_confirmation``'s own per-page
    ``noindex`` meta tag, this is a second layer, not the only one), and
    point crawlers at the sitemap.
    """
    settings = get_settings()
    base_url = settings.public_base_url.rstrip("/")
    body = (
        "User-agent: *\nAllow: /\nDisallow: /preview/\nDisallow: /order-confirmation/\n"
        f"Sitemap: {base_url}/sitemap.xml\n"
    )
    return PlainTextResponse(content=body)

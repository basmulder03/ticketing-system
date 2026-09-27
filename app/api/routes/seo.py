"""``/sitemap.xml`` and ``/robots.txt`` at the site root. Only published events
are listed; drafts and previews must never be indexed.
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
    """Every published event's ``/e/<slug>`` URL."""
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
    """Allow crawling, but disallow ``/preview/`` and ``/order-confirmation/``
    (both also set ``noindex`` themselves), and point to the sitemap.
    """
    settings = get_settings()
    base_url = settings.public_base_url.rstrip("/")
    body = (
        "User-agent: *\nAllow: /\nDisallow: /preview/\nDisallow: /order-confirmation/\n"
        f"Sitemap: {base_url}/sitemap.xml\n"
    )
    return PlainTextResponse(content=body)

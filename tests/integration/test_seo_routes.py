"""``/sitemap.xml`` and ``/robots.txt`` (``app.api.routes.seo``): only
published events are listed, draft/preview events are never indexed.
"""

import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable

from httpx import AsyncClient

from app.models.enums import PublishStatus
from app.models.event import Event


async def test_sitemap_includes_only_published_events(
    client: AsyncClient, make_event: Callable[..., Awaitable[Event]]
) -> None:
    published = await make_event(status=PublishStatus.PUBLISHED, slug="published-show")
    draft = await make_event(status=PublishStatus.DRAFT, slug="draft-show")

    response = await client.get("/sitemap.xml")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")

    body = response.text
    assert f"/events/{published.slug}" in body
    assert f"/events/{draft.slug}" not in body


async def test_sitemap_is_well_formed_xml(
    client: AsyncClient, make_event: Callable[..., Awaitable[Event]]
) -> None:
    await make_event(status=PublishStatus.PUBLISHED)

    response = await client.get("/sitemap.xml")
    root = ET.fromstring(response.text)
    assert root.tag.endswith("urlset")


async def test_robots_txt_disallows_preview_and_points_to_sitemap(client: AsyncClient) -> None:
    response = await client.get("/robots.txt")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert "Disallow: /preview/" in body
    assert "Sitemap:" in body
    assert "sitemap.xml" in body

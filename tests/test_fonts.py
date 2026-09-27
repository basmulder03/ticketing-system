"""Self-hosted ``ThemeFont`` assets (see ``app.models.enums.ThemeFont`` and
``app/static/fonts.css``): the stylesheet and every font file it references
are actually served, and every non-system font has one.
"""

import re
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from app.main import app
from app.models.enums import ThemeFont

_STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "static"

_NON_SYSTEM_FONTS = [font for font in ThemeFont if font not in (ThemeFont.SYSTEM_SANS, ThemeFont.SYSTEM_SERIF)]


def test_every_non_system_font_has_a_woff2_file_on_disk() -> None:
    for font in _NON_SYSTEM_FONTS:
        for weight in ("400", "700"):
            path = _STATIC_DIR / "fonts" / f"{font.value}-{weight}.woff2"
            assert path.is_file(), f"missing {path}"


async def test_fonts_css_is_served_and_declares_every_non_system_font() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/static/fonts.css")

    assert response.status_code == 200
    css = response.text
    for font in _NON_SYSTEM_FONTS:
        # Each weight/font pair's src file must actually be referenced.
        for weight in ("400", "700"):
            assert re.search(
                rf"font-weight:\s*{weight};[^}}]*src:\s*url\('/static/fonts/{font.value}-{weight}\.woff2'\)",
                css,
                re.DOTALL,
            ), f"{font.value} weight {weight} not declared"


async def test_fonts_css_woff2_files_referenced_all_exist_and_resolve() -> None:
    """Every ``src: url(...)`` in the stylesheet resolves to a real, servable file
    (catches a typo'd filename that ``test_fonts_css_is_served...`` wouldn't).
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        css_response = await client.get("/static/fonts.css")
        paths = re.findall(r"url\('(/static/fonts/[^']+)'\)", css_response.text)
        assert paths
        for path in paths:
            file_response = await client.get(path)
            assert file_response.status_code == 200, path

"""Regression coverage for a real bug found via the user's own manual
testing: the theme editor's and email-template editor's "live preview"
panes claimed to update automatically as fields were edited (see each
template's own hint text), but never actually did.

Root cause: both forms' ``hx-trigger`` carried a ``changed`` modifier
applied to the ``<form>`` element itself (no ``from:`` target). htmx's
``changed`` check compares ``element.value`` between events — a ``<form>``
has no meaningful ``.value`` (always ``undefined``), so the comparison was
always ``undefined === undefined``, silently swallowing EVERY trigger, no
request ever fired. Confirmed live via a real Playwright browser session
before fixing (zero ``preview-fragment`` requests on any field change);
``delay:`` alone already debounces rapid input, so the fix simply drops
``changed`` rather than replacing it with something else.

The existing ``preview-fragment`` route tests (``test_web_backoffice_routes.py``,
``test_web_email_templates_routes.py``) all POST directly to that route —
they would never have caught this, since the bug was entirely in whether
the BROWSER ever sends the request at all. Only a real browser-driven test
like this one exercises that path.

Reuses ``axe_page`` (a real Playwright ``Page`` wired to the in-process ASGI
app via request interception — see ``tests/integration/conftest.py``) even
though these tests don't run axe-core themselves; the fixture name is just
"a Playwright Page pointed at this app", not accessibility-specific.
"""

from collections.abc import Awaitable, Callable
from decimal import Decimal

from httpx import AsyncClient
from playwright.async_api import Page

from app.models.enums import PublishStatus, ThemeFont
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.show import Show
from app.models.theme import Theme
from app.models.ticket_type import TicketType
from tests.integration.conftest import SeededAdmin, apply_set_cookie_headers


async def _login(axe_page: Page, client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]) -> None:
    seeded = await make_admin_user()
    login = await client.post("/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password})
    assert login.status_code == 200
    session_cookies = login.headers.get_list("set-cookie")
    assert session_cookies
    await apply_set_cookie_headers(axe_page.context, session_cookies)


async def test_theme_editor_live_preview_updates_on_color_change_with_no_manual_save(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_theme: Callable[..., Awaitable[Theme]],
) -> None:
    await _login(axe_page, client, make_admin_user)
    event = await make_event(status=PublishStatus.PUBLISHED, name="Live Preview Event")
    await make_theme(
        event_id=event.id,
        primary_color="#111111",
        secondary_color="#eeeeee",
        accent_color="#c9a227",
        font_choice=ThemeFont.SYSTEM_SANS,
        status=PublishStatus.DRAFT,
    )

    await axe_page.goto(f"/events/{event.id}/theme")
    before = await axe_page.inner_html("#theme-preview-pane")
    assert "#c9a227" in before

    # A real field change, no manual save-button click -- <input type=color>
    # can't be driven via Playwright's .fill(), so this dispatches a
    # genuine 'input' event the same way a native color picker would.
    await axe_page.evaluate(
        """() => {
            const el = document.getElementById('accent_color');
            el.value = '#00ff00';
            el.dispatchEvent(new Event('input', {bubbles: true}));
        }"""
    )
    await axe_page.wait_for_timeout(900)

    after = await axe_page.inner_html("#theme-preview-pane")
    assert "#00ff00" in after, "live preview pane never updated after a field change"


async def test_theme_editor_live_preview_contrast_table_updates_on_color_change(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_theme: Callable[..., Awaitable[Theme]],
) -> None:
    """The same live-update bug also silently broke the contrast pass/fail
    table (another explicit complaint: "for color contrast is not live")
    since it's rendered by the identical preview-fragment swap."""
    await _login(axe_page, client, make_admin_user)
    event = await make_event(status=PublishStatus.PUBLISHED, name="Contrast Live Preview Event")
    await make_theme(
        event_id=event.id,
        primary_color="#111111",
        secondary_color="#eeeeee",
        accent_color="#c9a227",
        font_choice=ThemeFont.SYSTEM_SANS,
        status=PublishStatus.DRAFT,
    )

    await axe_page.goto(f"/events/{event.id}/theme")
    before_text = await axe_page.inner_text("#theme-preview-pane")

    # A near-white accent on a near-white secondary fails AA outright --
    # forces at least one "Fail" badge to appear if the table is genuinely
    # recalculating.
    await axe_page.evaluate(
        """() => {
            const el = document.getElementById('accent_color');
            el.value = '#fefefe';
            el.dispatchEvent(new Event('input', {bubbles: true}));
        }"""
    )
    await axe_page.wait_for_timeout(900)

    after_text = await axe_page.inner_text("#theme-preview-pane")
    assert after_text != before_text, "contrast table never recalculated after a field change"
    assert "Fail" in after_text


async def test_email_template_editor_live_preview_updates_on_subject_change(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    await _login(axe_page, client, make_admin_user)
    event = await make_event(status=PublishStatus.PUBLISHED, name="Email Live Preview Event")
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    await make_ticket_type(show_id=show.id, name="Adult", price=Decimal("15.00"))
    await make_event_config(event_id=event.id)

    await axe_page.goto(f"/events/{event.id}/email-templates")
    before = await axe_page.inner_html("#email-template-preview-pane")

    await axe_page.fill("#subject", "A Brand New Live-Updated Subject Line")
    await axe_page.wait_for_timeout(900)

    after = await axe_page.inner_html("#email-template-preview-pane")
    assert after != before, "email template live preview never updated after a field change"

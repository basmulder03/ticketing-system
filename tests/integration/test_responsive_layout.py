"""Automated responsive-layout regression checks (Milestone 9 hardening —
the "full responsive audit across breakpoints" line item) — per
PROJECT_BRIEF.md's Responsive & Multi-Device Display section: "tested at
common breakpoints (~375px, ~768px, ~1024px, ~1440px+)" and this app's own
stated hard requirement that the page body must never scroll horizontally
at any of them.

This project had per-milestone spot-checks before (e.g. a manual tablet-
width look at the Stats dashboard, Milestone 8) but no systematic,
automated pass across every page at every breakpoint — this file is that
pass. It reuses ``axe_page``/``apply_set_cookie_headers`` from
``conftest.py`` (the same real-ASGI-app-backed Playwright ``Page`` the
accessibility suite already uses) purely for its request-interception
wiring, not for axe itself: no ``run_axe`` call happens here, only
``page.set_viewport_size`` + a ``document.documentElement`` measurement.
Reusing the existing fixture (rather than adding a second browser-wiring
mechanism) was the deciding factor in "is a lightweight automated check
worth adding" — the marginal cost was small since the harness already
existed.

What this DOES check, automatically, for every page listed below: at each
of the four breakpoints, ``document.documentElement.scrollWidth`` never
exceeds ``document.documentElement.clientWidth`` (i.e. the page body itself
never grows a horizontal scrollbar). Each page is navigated to ONCE and then
resized in place across all four breakpoints (a real ``resize`` the browser
recalculates layout/media queries for), rather than four separate
navigations — real browsers behave identically either way for CSS media
queries, and this keeps the suite fast.

What this DOES NOT check (manual-only, see this milestone's
accessibility-auditor handoff for the full list): actual visual
overlap/clipping of text (only "does the outermost box overflow" is
measured, not whether inner content visually collides), real tap-target
size/spacing at 375px (WCAG 2.5.5/2.5.8 target-size needs either a
per-element geometry assertion this suite doesn't attempt, or a human
looking at a real touchscreen), and pixel-perfect design review (explicitly
out of scope per PROJECT_BRIEF.md's Testing section). Each page/breakpoint
combination was ALSO manually screenshotted and reviewed once during this
audit — see the handoff notes for what that manual pass found.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from httpx import AsyncClient
from playwright.async_api import Page

from app.models.enums import PaymentMethod, PublishStatus, ThemeFont
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.show import Show
from app.models.theme import Theme
from app.models.ticket_type import TicketType
from tests.integration.conftest import SeededAdmin, apply_set_cookie_headers

# PROJECT_BRIEF.md's own stated breakpoints, each paired with a plausible
# viewport height for that device class (irrelevant to the horizontal-
# scroll check itself, but a too-short height on a tall page would trigger
# Chromium's own vertical scrollbar reserving a few px of width -- using a
# generous height for every entry avoids that being mistaken for a real
# horizontal-overflow bug).
BREAKPOINTS: list[tuple[str, int, int]] = [
    ("375px (mobile)", 375, 900),
    ("768px (tablet)", 768, 1100),
    ("1024px (laptop)", 1024, 900),
    ("1440px (desktop)", 1440, 1000),
]


async def _assert_no_horizontal_scroll_across_breakpoints(page: Page, path: str) -> None:
    response = await page.goto(path)
    assert response is not None, f"navigation to {path} got no response at all"

    failures: list[str] = []
    for label, width, height in BREAKPOINTS:
        await page.set_viewport_size({"width": width, "height": height})
        metrics = await page.evaluate(
            """
            () => ({
              scrollWidth: document.documentElement.scrollWidth,
              clientWidth: document.documentElement.clientWidth,
            })
            """
        )
        if metrics["scrollWidth"] > metrics["clientWidth"]:
            failures.append(
                f"{path} at {label}: documentElement.scrollWidth={metrics['scrollWidth']} > "
                f"clientWidth={metrics['clientWidth']} (page body scrolls horizontally)"
            )

    assert failures == [], "\n".join(failures)


# --- Public site ------------------------------------------------------------


async def _seed_public_event(
    *,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
    make_theme: Callable[..., Awaitable[Theme]],
) -> tuple[Event, Show, TicketType]:
    """A realistic published Event: two Shows (exercises the show/date
    picker), a long event name (a real layout stress case at 375px), and a
    Theme -- same shape ``test_public_site_accessibility.py``'s
    ``_seed_real_event`` uses, duplicated locally per this project's
    "each test file owns its own setup helpers" convention."""
    event = await make_event(
        status=PublishStatus.PUBLISHED,
        name="Christmas Passion — A Very Long Event Name To Stress-Test Wrapping",
        description="A festive concert telling the Christmas story through music and narration.",
    )
    show_a = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED, venue_name="Het Kruispunt")
    await make_show(
        event_id=event.id,
        status=PublishStatus.PUBLISHED,
        venue_name="De Kapel",
        date=datetime.now(UTC).date() + timedelta(days=45),
    )
    ticket_type = await make_ticket_type(
        show_id=show_a.id, name="Adult", price=Decimal("15.00"), quantity_available=25
    )
    await make_event_config(
        event_id=event.id,
        sales_live_at=datetime.now(UTC) - timedelta(days=1),
        enabled_payment_methods=[PaymentMethod.MOLLIE, PaymentMethod.DOOR],
    )
    await make_theme(
        event_id=event.id,
        primary_color="#123abc",
        secondary_color="#fefefe",
        accent_color="#ff00ff",
        font_choice=ThemeFont.LORA,
        status=PublishStatus.PUBLISHED,
    )
    return event, show_a, ticket_type


async def test_landing_page_has_no_horizontal_scroll_at_any_breakpoint(
    axe_page: Page,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
    make_theme: Callable[..., Awaitable[Theme]],
) -> None:
    event, _show, _tt = await _seed_public_event(
        make_event=make_event,
        make_show=make_show,
        make_ticket_type=make_ticket_type,
        make_event_config=make_event_config,
        make_theme=make_theme,
    )

    await _assert_no_horizontal_scroll_across_breakpoints(axe_page, f"/e/{event.slug}")


async def test_order_confirmation_page_has_no_horizontal_scroll_at_any_breakpoint(
    axe_page: Page,
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, price=Decimal("15.00"), quantity_available=10)
    await make_event_config(
        event_id=event.id,
        sales_live_at=datetime.now(UTC) - timedelta(days=1),
        enabled_payment_methods=[PaymentMethod.DOOR],
    )

    checkout = await client.post(
        f"/e/{event.slug}/checkout",
        data={
            "show_choice": str(show.id),
            f"qty_{ticket_type.id}": "2",
            "buyer_name": "Buyer Name",
            "buyer_email": "buyer@example.test",
            "buyer_address": "1 Test Street",
            "payment_method": "door",
        },
    )
    assert checkout.status_code == 303
    order_confirmation_path = checkout.headers["location"]
    set_cookie_values = checkout.headers.get_list("set-cookie")
    assert set_cookie_values, "checkout redirect set no cookies to hand off to the browser"
    await apply_set_cookie_headers(axe_page.context, set_cookie_values)

    await _assert_no_horizontal_scroll_across_breakpoints(axe_page, order_confirmation_path)


async def test_privacy_policy_page_has_no_horizontal_scroll_at_any_breakpoint(axe_page: Page) -> None:
    await _assert_no_horizontal_scroll_across_breakpoints(axe_page, "/privacy")


async def test_beamer_page_has_no_horizontal_scroll_at_any_breakpoint(
    axe_page: Page,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_theme: Callable[..., Awaitable[Theme]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED, name="Beamer Responsive Event")
    await make_show(
        event_id=event.id,
        status=PublishStatus.PUBLISHED,
        date=datetime.now(UTC).date() + timedelta(days=30),
        venue_name="Het Kruispunt",
    )
    await make_theme(event_id=event.id, font_choice=ThemeFont.LORA, status=PublishStatus.PUBLISHED)

    await _assert_no_horizontal_scroll_across_breakpoints(axe_page, f"/e/{event.slug}/beamer")


# --- Backoffice ---------------------------------------------------------


async def _login_and_apply_session_cookie(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
) -> None:
    seeded = await make_admin_user()
    login = await client.post(
        "/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password}
    )
    assert login.status_code == 200
    session_cookies = login.headers.get_list("set-cookie")
    assert session_cookies, "admin login set no session cookie to hand off to the browser"
    await apply_set_cookie_headers(axe_page.context, session_cookies)


async def test_login_page_has_no_horizontal_scroll_at_any_breakpoint(
    axe_page: Page, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    # Post-launch fix: GET /login redirects to /setup while zero AdminUser
    # rows exist at all (see app.web.routes.auth module docstring) — seed
    # one first (without logging in as them) so this test actually reaches
    # the login page it's named for, not /setup.
    await make_admin_user()
    await _assert_no_horizontal_scroll_across_breakpoints(axe_page, "/login")


async def test_events_list_page_has_no_horizontal_scroll_at_any_breakpoint(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
) -> None:
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user)
    await make_event(status=PublishStatus.PUBLISHED, name="Responsive Events List Event")

    await _assert_no_horizontal_scroll_across_breakpoints(axe_page, "/events")


async def test_theme_editor_page_has_no_horizontal_scroll_at_any_breakpoint(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
) -> None:
    """Includes the two-column form/live-preview layout
    (``.bo-theme-layout``) that has the most to prove at 375px, where it
    must collapse to a single column rather than squeeze both side by
    side."""
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user)
    event = await make_event(status=PublishStatus.PUBLISHED, name="Responsive Theme Event")

    await _assert_no_horizontal_scroll_across_breakpoints(axe_page, f"/events/{event.id}/theme")


async def test_email_template_editor_page_has_no_horizontal_scroll_at_any_breakpoint(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
) -> None:
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user)
    event = await make_event(status=PublishStatus.PUBLISHED, name="Responsive Email Template Event")

    await _assert_no_horizontal_scroll_across_breakpoints(axe_page, f"/events/{event.id}/email-templates")


async def test_orders_list_page_has_no_horizontal_scroll_at_any_breakpoint(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """The Orders table is this page's widest content (7 columns) -- the
    most realistic table to prove ``.bo-table-wrap``'s ``overflow-x: auto``
    scroll-container fallback actually keeps the page BODY itself from
    scrolling, per this app's hard requirement."""
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user)
    event = await make_event(status=PublishStatus.PUBLISHED, name="Responsive Orders Event")
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=5)
    await make_event_config(event_id=event.id, enabled_payment_methods=[PaymentMethod.DOOR])

    checkout = await client.post(
        "/api/v1/public/checkout",
        json={
            "buyer_name": "Responsive Buyer",
            "buyer_email": "responsive-buyer@example.test",
            "buyer_address": "1 Test Street",
            "language": "en",
            "payment_method": "door",
            "items": [{"ticket_type_id": str(ticket_type.id), "quantity": 1}],
        },
    )
    assert checkout.status_code == 201, checkout.text

    await _assert_no_horizontal_scroll_across_breakpoints(axe_page, f"/events/{event.id}/orders")


async def test_stats_page_has_no_horizontal_scroll_at_any_breakpoint(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
) -> None:
    """PROJECT_BRIEF.md's Responsive & Multi-Device section explicitly
    calls out that the Stats dashboard "should remain usable on a tablet at
    minimum" -- the 768px breakpoint here is that requirement, checked
    automatically rather than only by the Milestone 8 manual spot-check."""
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user)
    event = await make_event(status=PublishStatus.PUBLISHED, name="Responsive Stats Event")
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    await make_ticket_type(show_id=show.id, name="Adult", quantity_available=20)

    await _assert_no_horizontal_scroll_across_breakpoints(axe_page, f"/events/{event.id}/stats")

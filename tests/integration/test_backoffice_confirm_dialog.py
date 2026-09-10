"""Behavioral coverage for the styled confirm dialog
(``app/templates/backoffice/base.html``'s ``#bo-confirm-dialog``) that
replaced ``window.confirm()`` for every ``data-confirm`` form (post-launch
fix, per the user's NOTES: "Native confirms/popups/alerts thingies have to
go. Keep with the style").

Drives a real Playwright browser against the in-process ASGI app (the
``axe_page`` fixture — see ``tests/integration/conftest.py``), since the
whole point of this component is real DOM/event behavior (a click opening
a native ``<dialog>``, ESC closing it, the pending form only actually
submitting after a real "Confirm" click) that no direct-HTTP-POST test
could ever exercise.

Uses the event-edit page's "Delete event" danger-zone form as the
real-world ``data-confirm`` example throughout (any of the other six forms
using the same shared dialog would do just as well — see
``app/templates/backoffice/base.html``'s script for the single, generic
implementation every one of them shares). The trigger button is selected
via ``form[data-confirm] button[type=submit]`` rather than a text locator
-- a loose ``text="Delete event"`` locator turned out to ALSO match this
page's own ``<h1>Edit: {event.name}</h1>`` heading whenever a test used an
event name that happened to contain "Delete Event" as a case-insensitive
substring (Playwright's plain ``page.click("text=...")`` isn't in strict
mode and silently clicks the first match), which was the real cause of an
extremely confusing false test failure while first writing these tests —
the dialog/form implementation itself was correct the entire time.
"""

from collections.abc import Awaitable, Callable

from httpx import AsyncClient
from playwright.async_api import Page

from app.models.enums import PublishStatus
from app.models.event import Event
from tests.integration.conftest import SeededAdmin, apply_set_cookie_headers

_DELETE_TRIGGER = 'form[data-confirm] button[type="submit"]'


async def _login(axe_page: Page, client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]) -> None:
    seeded = await make_admin_user()
    login = await client.post("/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password})
    assert login.status_code == 200
    session_cookies = login.headers.get_list("set-cookie")
    assert session_cookies
    await apply_set_cookie_headers(axe_page.context, session_cookies)


async def test_clicking_the_danger_button_opens_the_dialog_not_a_native_confirm(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
) -> None:
    await _login(axe_page, client, make_admin_user)
    event = await make_event(status=PublishStatus.PUBLISHED, name="Confirm Dialog Event")

    await axe_page.goto(f"/events/{event.id}/edit")
    await axe_page.click(_DELETE_TRIGGER)

    is_open = await axe_page.evaluate("() => document.getElementById('bo-confirm-dialog').open")
    assert is_open is True
    message = await axe_page.inner_text("#bo-confirm-dialog-message")
    assert event.name in message


async def test_cancel_closes_the_dialog_without_submitting_the_form(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
) -> None:
    await _login(axe_page, client, make_admin_user)
    event = await make_event(status=PublishStatus.PUBLISHED, name="Cancel Dialog Event")

    await axe_page.goto(f"/events/{event.id}/edit")
    await axe_page.click(_DELETE_TRIGGER)
    await axe_page.click("#bo-confirm-dialog button[value=cancel]")
    await axe_page.wait_for_timeout(200)

    is_open = await axe_page.evaluate("() => document.getElementById('bo-confirm-dialog').open")
    assert is_open is False
    # Still on the edit page -- the destructive form was never submitted.
    assert axe_page.url.endswith(f"/events/{event.id}/edit")

    still_exists = await client.get(f"/api/v1/events/{event.id}")
    assert still_exists.status_code == 200


async def test_escape_key_closes_the_dialog_without_submitting(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
) -> None:
    """A free benefit of building this on the native <dialog> element
    (showModal()) rather than a hand-rolled overlay: ESC-to-close comes
    from the browser itself, no extra JS needed."""
    await _login(axe_page, client, make_admin_user)
    event = await make_event(status=PublishStatus.PUBLISHED, name="Escape Dialog Event")

    await axe_page.goto(f"/events/{event.id}/edit")
    await axe_page.click(_DELETE_TRIGGER)
    await axe_page.keyboard.press("Escape")
    await axe_page.wait_for_timeout(200)

    is_open = await axe_page.evaluate("() => document.getElementById('bo-confirm-dialog').open")
    assert is_open is False

    still_exists = await client.get(f"/api/v1/events/{event.id}")
    assert still_exists.status_code == 200


async def test_confirm_actually_submits_the_pending_form(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
) -> None:
    """``no_wait_after=True`` plus a short explicit ``timeout=``: this
    click closes the native ``<dialog>`` (a synchronous, browser-internal
    step) and THEN, from inside the dialog's own "close" event handler,
    calls ``pendingForm.requestSubmit()`` to actually fire the real
    POST+redirect -- Playwright's own post-click navigation-wait heuristic
    doesn't reliably observe a navigation kicked off that indirectly under
    this fixture's ASGI request-interception harness (confirmed against
    the real dev server: the exact same user action there completes and
    redirects normally). The real assertion below (a genuine 404 on
    re-fetch) is what actually proves the submit happened, regardless of
    what the browser's own URL bar shows in this harness."""
    await _login(axe_page, client, make_admin_user)
    event = await make_event(status=PublishStatus.PUBLISHED, name="Confirm Submit Event")

    await axe_page.goto(f"/events/{event.id}/edit")
    await axe_page.click(_DELETE_TRIGGER)
    await axe_page.wait_for_timeout(300)
    await axe_page.locator("#bo-confirm-dialog button[value=confirm]").click(no_wait_after=True, timeout=5000)
    await axe_page.wait_for_timeout(1500)

    deleted = await client.get(f"/api/v1/events/{event.id}")
    assert deleted.status_code == 404

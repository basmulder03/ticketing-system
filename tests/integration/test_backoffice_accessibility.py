"""Automated WCAG 2.1 AA checks (axe-core, via Playwright) against the real
rendered backoffice Orders-list page — per PROJECT_BRIEF.md's Testing
section ("Accessibility tests: automated AA checks (e.g. axe-core) run
against public pages ... as part of the test suite") and this milestone's
accessibility-auditor pass on Milestone 6's new backoffice markup (the
"mark as paid" inline form in ``app/templates/backoffice/orders_list.html``).

Mirrors ``test_public_site_accessibility.py``'s fixtures/conventions
(``axe_page``/``run_axe``/``format_axe_violations`` from ``conftest.py``,
seeding through the same ``make_event``/``make_show``/... factories) rather
than introducing a second accessibility-testing approach. This is the FIRST
axe sweep of any backoffice page — prior milestones only audited the public
site and outgoing email HTML (see those two files' module docstrings); the
backoffice's other pages (login, theme editor, email-template editor,
events list) are NOT covered here and remain an open gap — see this
milestone's accessibility-auditor handoff for that flagged item. This file's
own scope is deliberately just the page Milestone 6 actually changed.

Login is performed via a plain ``httpx`` client hitting
``POST /api/v1/auth/login`` (same pattern ``test_web_orders_routes.py`` and
``test_mark_order_paid_route.py`` use), with the resulting session cookie
handed to ``axe_page``'s browser context via ``apply_set_cookie_headers`` —
identical technique to how ``test_public_site_accessibility.py``'s
order-confirmation test hands off a checkout-issued cookie, and for the same
reason (a real form-click login isn't needed to audit THIS page's HTML, and
driving it through ``axe_page`` would just add an extra CSRF/login-form
round trip this suite doesn't otherwise need).

Color-contrast scope note: unlike the public landing page (which disables
axe's ``color-contrast`` rule because a Theme's fixed colors are audited
separately by ``app.services.contrast``), the backoffice has no per-event
Theme at all — every color here comes from ``app/static/backoffice.css``'s
fixed ``:root`` tokens, so ``color-contrast`` stays fully enabled, same as
the (also Theme-free) order-confirmation/404 pages.

Manual-only items NOT covered by this automated suite: real screen-reader
behavior, and real Tab-key traversal order (this page's tab order is
straightforward top-to-bottom/left-to-right table markup with no CSS
reordering, so a dedicated keyboard-traversal test — like the public
checkout form's — wasn't judged necessary; flagged as a manual checklist
item instead of asserted here).
"""

import uuid
from collections.abc import Awaitable, Callable

from httpx import AsyncClient
from playwright.async_api import Page

from app.models.enums import PaymentMethod, PublishStatus
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.show import Show
from app.models.ticket_type import TicketType
from tests.integration.conftest import (
    SeededAdmin,
    apply_set_cookie_headers,
    format_axe_violations,
    run_axe,
)


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


async def test_orders_list_page_has_no_axe_violations(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """Both real per-row action branches on the same page in one pass: a
    ``paid`` order (renders the pre-existing resend-confirmation-email
    form/download-invoice link) and a ``pending_door`` order (renders
    Milestone 6's new mark-as-paid form, with its two labeled text inputs)
    — exactly the two states ``app/templates/backoffice/orders_list.html``
    branches on."""
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user)

    event = await make_event(status=PublishStatus.PUBLISHED, name="Backoffice A11y Event")
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    door_ticket_type = await make_ticket_type(show_id=show.id, quantity_available=5)
    paid_ticket_type = await make_ticket_type(show_id=show.id, quantity_available=5)
    await make_event_config(event_id=event.id, enabled_payment_methods=[PaymentMethod.DOOR])

    door_response = await client.post(
        "/api/v1/public/checkout",
        json={
            "buyer_name": "Door Buyer",
            "buyer_email": f"door-{uuid.uuid4().hex}@example.test",
            "buyer_address": "1 Test Street",
            "language": "en",
            "payment_method": "door",
            "items": [{"ticket_type_id": str(door_ticket_type.id), "quantity": 1}],
        },
    )
    assert door_response.status_code == 201, door_response.text
    assert door_response.json()["status"] == "pending_door"

    paid_checkout = await client.post(
        "/api/v1/public/checkout",
        json={
            "buyer_name": "Paid Buyer",
            "buyer_email": f"paid-{uuid.uuid4().hex}@example.test",
            "buyer_address": "1 Test Street",
            "language": "en",
            "payment_method": "door",
            "items": [{"ticket_type_id": str(paid_ticket_type.id), "quantity": 1}],
        },
    )
    assert paid_checkout.status_code == 201, paid_checkout.text
    paid_order_id = paid_checkout.json()["id"]
    mark_paid = await client.post(
        f"/api/v1/orders/{paid_order_id}/mark-paid", json={"method_label": "cash"}
    )
    assert mark_paid.status_code == 200, mark_paid.text

    violations = await run_axe(axe_page, f"/events/{event.id}/orders")

    assert violations == [], format_axe_violations(violations)


async def test_orders_list_page_with_no_orders_has_no_axe_violations(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
) -> None:
    """The empty-state row ("No orders yet for this event.") is a distinct
    render path (a single ``<td colspan="7">`` in place of the whole
    per-order-row loop) — audited separately rather than assuming the
    populated-table test above implicitly covers it."""
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user)
    event = await make_event(status=PublishStatus.PUBLISHED, name="No Orders Yet Event")

    violations = await run_axe(axe_page, f"/events/{event.id}/orders")

    assert violations == [], format_axe_violations(violations)

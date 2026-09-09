"""Automated WCAG 2.1 AA checks (axe-core, via Playwright) against real
rendered backoffice pages — per PROJECT_BRIEF.md's Testing section
("Accessibility tests: automated AA checks (e.g. axe-core) run against
public pages ... as part of the test suite"). Originally added for the
accessibility-auditor pass on Milestone 6's new backoffice markup (the
"mark as paid" inline form in ``app/templates/backoffice/orders_list.html``)
and extended for the Milestone 8 Stats & Reporting dashboard
(``app/templates/backoffice/event_stats.html``) — same file, same
conventions, rather than a parallel test module per backoffice page.

Mirrors ``test_public_site_accessibility.py``'s fixtures/conventions
(``axe_page``/``run_axe``/``format_axe_violations`` from ``conftest.py``,
seeding through the same ``make_event``/``make_show``/... factories) rather
than introducing a second accessibility-testing approach. This was the FIRST
axe sweep of any backoffice page — prior milestones only audited the public
site and outgoing email HTML (see those two files' module docstrings); the
backoffice's other pages (login, theme editor, email-template editor,
events list) are still NOT covered here and remain an open gap — see this
milestone's accessibility-auditor handoff for that flagged item. This file's
scope is deliberately just the pages an accessibility-auditor pass has
actually reviewed (Milestones 6 and 8's new/changed markup), not a sweep of
every backoffice page that happens to exist.

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

Milestone 9 hardening pass: extended (per that milestone's
accessibility-auditor handoff) to close the previously-flagged gap that
login, the theme editor, the email-template editor, and the events list had
NO automated axe coverage at all — see the "Login / Theme editor / Email
template editor / Events list" sections below. Same fixtures/conventions as
the rest of this file (``_login_and_apply_session_cookie``, ``run_axe``,
``format_axe_violations``) — no new tooling introduced.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal

from httpx import AsyncClient
from playwright.async_api import Page
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.email_template import EmailTemplate
from app.models.enums import EmailTemplateType, OrderStatus, PaymentMethod, PublishStatus, ThemeFont
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.order import Order
from app.models.show import Show
from app.models.theme import Theme
from app.models.ticket import Ticket
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


# --- Milestone 8: Stats & Reporting dashboard -------------------------------


async def _make_order_with_tickets(
    session: AsyncSession,
    *,
    event_id: uuid.UUID,
    ticket_type_id: uuid.UUID,
    status: OrderStatus,
    payment_method: PaymentMethod = PaymentMethod.DOOR,
    quantity: int = 1,
    total: Decimal = Decimal("10.00"),
    mollie_payment_id: str | None = None,
    scanned_quantity: int = 0,
) -> Order:
    """Insert an Order with ``quantity`` Ticket rows directly, bypassing
    checkout — same helper/shape as
    ``tests/integration/test_stats_route.py``'s helper of the same name,
    duplicated locally rather than imported across test modules (this
    project's existing convention — each test file owns its own setup
    helpers). The first ``scanned_quantity`` tickets get a real
    ``scanned_at``, so this can seed both a nonzero revenue-split bar and a
    nonzero scan-in count for the axe sweep below."""
    order = Order(
        event_id=event_id,
        buyer_name="Buyer",
        buyer_email=f"buyer-{uuid.uuid4().hex}@example.test",
        buyer_address="1 Test Street",
        status=status,
        payment_method=payment_method,
        mollie_payment_id=mollie_payment_id,
        total=total,
        language="en",
    )
    session.add(order)
    await session.flush()
    for index in range(quantity):
        ticket = Ticket(order_id=order.id, ticket_type_id=ticket_type_id)
        if index < scanned_quantity:
            ticket.scanned_at = datetime.now(UTC)
        session.add(ticket)
    await session.commit()
    await session.refresh(order)
    return order


async def test_stats_page_has_no_axe_violations(
    axe_page: Page,
    client: AsyncClient,
    db_session: AsyncSession,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
) -> None:
    """The fully-populated render path: headline stat cards, a two-entry
    (mollie + door, both nonzero so the revenue-split bar actually renders a
    fill) revenue-by-payment-method breakdown, the CSV export link, and one
    Show's ``<details open>`` disclosure with a real ticket-type table
    (including a scanned ticket, so "Scanned" is nonzero too) — exercises
    every distinct piece of markup this milestone's accessibility-auditor
    pass reviewed (the ``<details>``/``<summary>`` disclosure, the revenue
    bar, and the per-show table's ``<caption>``/``<th scope="col">``
    structure) in one pass."""
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user)

    event = await make_event(status=PublishStatus.PUBLISHED, name="Stats A11y Event")
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    mollie_ticket_type = await make_ticket_type(show_id=show.id, name="Adult", quantity_available=20)
    door_ticket_type = await make_ticket_type(show_id=show.id, name="Child", quantity_available=20)

    await _make_order_with_tickets(
        db_session,
        event_id=event.id,
        ticket_type_id=mollie_ticket_type.id,
        status=OrderStatus.PAID,
        payment_method=PaymentMethod.MOLLIE,
        quantity=2,
        total=Decimal("40.00"),
        mollie_payment_id="tr_stats_a11y",
        scanned_quantity=1,
    )
    await _make_order_with_tickets(
        db_session,
        event_id=event.id,
        ticket_type_id=door_ticket_type.id,
        status=OrderStatus.PAID,
        payment_method=PaymentMethod.DOOR,
        quantity=1,
        total=Decimal("10.00"),
    )
    await _make_order_with_tickets(
        db_session,
        event_id=event.id,
        ticket_type_id=door_ticket_type.id,
        status=OrderStatus.PENDING_DOOR,
        payment_method=PaymentMethod.DOOR,
        quantity=1,
        total=Decimal("10.00"),
    )

    violations = await run_axe(axe_page, f"/events/{event.id}/stats")

    assert violations == [], format_axe_violations(violations)


async def test_stats_page_with_no_shows_has_no_axe_violations(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
) -> None:
    """The empty-state render path ("No shows yet for this event.", and a
    revenue-by-payment-method split that is present but entirely zeroed) —
    a distinct branch from the populated test above, audited separately for
    the same reason ``test_orders_list_page_with_no_orders_has_no_axe_violations``
    is."""
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user)
    event = await make_event(status=PublishStatus.PUBLISHED, name="No Shows Yet Event")

    violations = await run_axe(axe_page, f"/events/{event.id}/stats")

    assert violations == [], format_axe_violations(violations)


# --- Milestone 9: login, theme editor, email-template editor, events list --
#
# Previously-flagged gap (see this file's module docstring): these four
# pages had no automated axe sweep at all, unlike Orders/Stats above. Same
# ``_login_and_apply_session_cookie``/``run_axe``/``format_axe_violations``
# conventions throughout.


async def test_login_page_has_no_axe_violations(axe_page: Page) -> None:
    """Unauthenticated GET /login — the default, no-error render path."""
    violations = await run_axe(axe_page, "/login")

    assert violations == [], format_axe_violations(violations)


async def test_login_page_error_state_has_no_axe_violations(axe_page: Page) -> None:
    """The ``role="alert"`` invalid-credentials banner
    (``app/templates/backoffice/login.html``), reached by really submitting
    the form with wrong credentials through Playwright (this page has no
    prerequisite seeded state, so a real form click — rather than
    ``httpx`` + cookie handoff, as the orders/stats tests above use for
    pages that DO need seeded data — is the simplest way to reach this
    branch)."""
    await axe_page.goto("/login")
    await axe_page.fill("#email", "nobody@example.test")
    await axe_page.fill("#password", "wrong-password")
    await axe_page.click('button[type="submit"]')
    await axe_page.wait_for_selector('[role="alert"]')

    # Not using run_axe() here: it navigates first, which would lose the
    # error state just reached above — evaluate axe directly against the
    # page as it currently sits instead (same pattern
    # test_public_site_accessibility.py's checkout-error-state test uses).
    result = await axe_page.evaluate(
        """
        async () => {
          return await axe.run(document, {
            runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa'] },
          });
        }
        """
    )
    assert result["violations"] == [], format_axe_violations(result["violations"])


async def test_theme_editor_page_has_no_axe_violations(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
) -> None:
    """No Theme row yet — the "fresh event, all defaults" render path
    (``theme`` is ``None`` throughout ``theme_editor.html``)."""
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user)
    event = await make_event(status=PublishStatus.PUBLISHED, name="Theme A11y Event")

    violations = await run_axe(axe_page, f"/events/{event.id}/theme")

    assert violations == [], format_axe_violations(violations)


async def test_theme_editor_page_with_saved_theme_and_active_custom_css_has_no_axe_violations(
    axe_page: Page,
    client: AsyncClient,
    db_session: AsyncSession,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """A saved Theme with active custom CSS — exercises the "Custom CSS is
    active" info banner, the open ``<details>`` advanced panel, and (via a
    second Event to copy from) the "Duplicate theme from another event"
    form's populated ``<select>``."""
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user)
    await make_event(status=PublishStatus.PUBLISHED, name="Source Event")
    event = await make_event(status=PublishStatus.PUBLISHED, name="Themed Event")
    await make_event_config(event_id=event.id)
    theme = Theme(
        event_id=event.id,
        primary_color="#1a1a1a",
        secondary_color="#ffffff",
        accent_color="#c9a227",
        font_choice=ThemeFont.LORA,
        custom_css="h1 { color: #123456; }",
        status=PublishStatus.PUBLISHED,
    )
    db_session.add(theme)
    await db_session.commit()

    violations = await run_axe(axe_page, f"/events/{event.id}/theme")

    assert violations == [], format_axe_violations(violations)


async def test_email_template_editor_page_has_no_axe_violations(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
) -> None:
    """No saved customization for this language yet — the "no customized
    template yet" info banner render path, with the Reset button in its
    ``disabled`` state."""
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user)
    event = await make_event(status=PublishStatus.PUBLISHED, name="Email Template A11y Event")

    violations = await run_axe(axe_page, f"/events/{event.id}/email-templates")

    assert violations == [], format_axe_violations(violations)


async def test_email_template_editor_page_with_saved_template_has_no_axe_violations(
    axe_page: Page,
    client: AsyncClient,
    db_session: AsyncSession,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
) -> None:
    """A saved customization for this language — the info banner is absent
    and the Reset button is enabled, a distinct branch from the test
    above."""
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user)
    event = await make_event(status=PublishStatus.PUBLISHED, name="Customized Email Template Event")
    template = EmailTemplate(
        event_id=event.id,
        language="en",
        template_type=EmailTemplateType.ORDER_CONFIRMATION_TICKET.value,
        subject="Your tickets for {{show_date}}",
        body="<p>Thanks for your order, {{buyer_name}}!</p>",
    )
    db_session.add(template)
    await db_session.commit()

    violations = await run_axe(axe_page, f"/events/{event.id}/email-templates?language=en")

    assert violations == [], format_axe_violations(violations)


async def test_events_list_page_has_no_axe_violations(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
) -> None:
    """At least one real Event row — the populated-table render path."""
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user)
    await make_event(status=PublishStatus.PUBLISHED, name="Events List A11y Event")

    violations = await run_axe(axe_page, "/events")

    assert violations == [], format_axe_violations(violations)


async def test_events_list_page_with_no_events_has_no_axe_violations(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
) -> None:
    """No Event rows at all — the empty-state ``<tr><td colspan="7">``
    render path, a distinct branch from the populated test above."""
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user)

    violations = await run_axe(axe_page, "/events")

    assert violations == [], format_axe_violations(violations)

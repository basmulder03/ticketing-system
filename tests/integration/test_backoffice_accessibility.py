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
from app.web.csrf import CSRF_COOKIE_NAME
from tests.integration.conftest import (
    SeededAdmin,
    SeededAgent,
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


async def test_orders_list_page_with_erased_and_invoiced_orders_has_no_axe_violations(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """Post-Milestone-9 GDPR erasure UI (see ``app/templates/backoffice/
    orders_list.html``'s ``data-confirm`` forms and
    ``app/templates/backoffice/base.html``'s delegated submit listener,
    added in commit 1be315c): two rows this suite's other Orders tests
    don't otherwise reach —

    1. A ``paid`` order that has NOT been erased yet: renders three danger/
       non-danger forms side by side ("Resend confirmation email", "Erase
       buyer PII", and — because it has an issued invoice — "Erase anyway
       (has an invoice)"). ``test_orders_list_page_has_no_axe_violations``
       above incidentally already exercises this combination via its own
       paid order, but this test seeds it explicitly so it survives even if
       that test's shape changes.
    2. The SAME order after actually calling the real erase-pii web route
       (not a hand-seeded ``buyer_name`` — exercising the real
       redirect-then-reload path) — the "Buyer details erased." plain-text
       indicator branch, which no existing axe test reaches at all.

    axe-core cannot evaluate the ``window.confirm()`` dialog itself (it's
    OS/browser chrome, not page DOM) — see this milestone's
    accessibility-auditor handoff for why that's fine here and what remains
    a manual-only check.
    """
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user)

    event = await make_event(status=PublishStatus.PUBLISHED, name="Erasure A11y Event")
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=5)
    await make_event_config(event_id=event.id, enabled_payment_methods=[PaymentMethod.DOOR])

    checkout = await client.post(
        "/api/v1/public/checkout",
        json={
            "buyer_name": "Erasure Buyer",
            "buyer_email": f"erasure-{uuid.uuid4().hex}@example.test",
            "buyer_address": "1 Test Street",
            "language": "en",
            "payment_method": "door",
            "items": [{"ticket_type_id": str(ticket_type.id), "quantity": 1}],
        },
    )
    assert checkout.status_code == 201, checkout.text
    order_id = checkout.json()["id"]
    mark_paid = await client.post(f"/api/v1/orders/{order_id}/mark-paid", json={"method_label": "cash"})
    assert mark_paid.status_code == 200, mark_paid.text

    orders_url = f"/events/{event.id}/orders"

    # Branch 1: paid + invoiced + not-yet-erased — both erase forms visible.
    violations = await run_axe(axe_page, orders_url)
    assert violations == [], format_axe_violations(violations)

    # Branch 2: same order, actually erased via the real web route (not a
    # hand-seeded buyer_name) — the "Buyer details erased." indicator.
    page = await client.get(orders_url)
    assert page.status_code == 200
    csrf_token = client.cookies.get(CSRF_COOKIE_NAME)
    assert csrf_token
    erase = await client.post(
        f"/events/{event.id}/orders/{order_id}/erase-pii",
        data={"csrf_token": csrf_token, "confirm": "true"},
    )
    assert erase.status_code == 303, erase.text

    violations = await run_axe(axe_page, orders_url)
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


# --- backoffice/core-management-ui branch: Event CRUD, EventConfig settings,
# Show/TicketType management, agent-account + audit-log UI, admin-user
# management. First accessibility-auditor pass over these six pages — see
# this branch's accessibility-auditor handoff for the violations found and
# fixed (per-event nav inconsistency on Shows, an unlabeled preview-link
# input, the SMTP/Mollie "test" cross-referenced-via-``form=""`` fields,
# the audit log's auto-submitting ``<select>``, and the target-id
# ``title``-only truncation) prior to this coverage being added. Same
# ``_login_and_apply_session_cookie``/``run_axe``/``format_axe_violations``
# conventions as the rest of this file throughout.


async def test_event_new_page_has_no_axe_violations(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
) -> None:
    """The blank "create event" form — no prerequisite seeded state."""
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user)

    violations = await run_axe(axe_page, "/events/new")

    assert violations == [], format_axe_violations(violations)


async def test_event_edit_page_has_no_axe_violations(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
) -> None:
    """The full edit page in one pass: the edit form, the preview-link
    copy-to-clipboard affordance (``#preview-link-input`` + the shared
    ``.bo-copy-link`` button), and the delete-event danger-zone form. axe-
    core cannot evaluate the danger zone's ``window.confirm()`` dialog
    itself (OS/browser chrome, not page DOM) — see this milestone's
    accessibility-auditor handoff for why that's a manual-only checklist
    item, same reasoning as the Orders list's erase-PII confirm() already
    documented above."""
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user)
    event = await make_event(status=PublishStatus.PUBLISHED, name="Edit Page A11y Event")

    violations = await run_axe(axe_page, f"/events/{event.id}/edit")

    assert violations == [], format_axe_violations(violations)


async def test_event_config_page_with_no_saved_config_has_no_axe_violations(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
) -> None:
    """No EventConfig row yet — every secret field's "no secret set"
    placeholder branch, and "no other events exist yet to copy settings
    from" (only one Event exists)."""
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user)
    event = await make_event(status=PublishStatus.PUBLISHED, name="Config A11y Event — fresh")

    violations = await run_axe(axe_page, f"/events/{event.id}/config")

    assert violations == [], format_axe_violations(violations)


async def test_event_config_page_with_saved_config_and_other_events_has_no_axe_violations(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """A saved EventConfig with secrets set (every "a secret is currently
    set — leave blank to keep it" placeholder branch, exercised via a real
    SMTP password/Mollie key), a nonzero ``sales_live_at``, and a second
    Event to populate the "copy settings from another event" ``<select>`` —
    also exercises the ``aria-describedby`` link between the SMTP/Mollie
    "test" fields (cross-referenced onto their hidden forms via the HTML5
    ``form=""`` attribute) and the "Send test email"/"Test Mollie
    connection" buttons near the bottom of the page."""
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user)
    await make_event(status=PublishStatus.PUBLISHED, name="Source Event For Copy")
    event = await make_event(status=PublishStatus.PUBLISHED, name="Config A11y Event — saved")
    await make_event_config(
        event_id=event.id,
        smtp_password="s3cret",
        mollie_test_api_key="test_abc123",
        mollie_live_api_key="live_abc123",
        sales_live_at=datetime.now(UTC),
        enabled_payment_methods=[PaymentMethod.MOLLIE, PaymentMethod.DOOR],
    )

    violations = await run_axe(axe_page, f"/events/{event.id}/config")

    assert violations == [], format_axe_violations(violations)


async def test_shows_manage_page_with_open_show_and_ticket_type_has_no_axe_violations(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
) -> None:
    """The "Add a show" form, one Show expanded via ``?open=<show_id>``
    (mirroring the Stats dashboard's ``<details open>`` per-show pattern —
    see this file's Milestone 8 section above), its "Edit show"/"Delete
    show" forms, and its ticket-types table with one real TicketType row."""
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user)
    event = await make_event(status=PublishStatus.PUBLISHED, name="Shows A11y Event")
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    await make_ticket_type(show_id=show.id)

    violations = await run_axe(axe_page, f"/events/{event.id}/shows?open={show.id}")

    assert violations == [], format_axe_violations(violations)


async def test_shows_manage_page_with_no_shows_has_no_axe_violations(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
) -> None:
    """No Show rows at all — the "No shows yet for this event" empty-state
    branch, a distinct render path from the populated test above."""
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user)
    event = await make_event(status=PublishStatus.PUBLISHED, name="No Shows Yet Event")

    violations = await run_axe(axe_page, f"/events/{event.id}/shows")

    assert violations == [], format_axe_violations(violations)


async def test_shows_manage_page_with_ticket_type_edit_and_add_details_expanded_has_no_axe_violations(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
) -> None:
    """Real interaction (not just ``run_axe``'s default-collapsed-state
    navigation) to expand BOTH nested ``<details>`` a screen reader user
    could reach on this page: an existing ticket type's "Edit ..." panel
    and the "Add a ticket type" panel — per this milestone's
    accessibility-auditor task, checking that the ADD form (not just the
    pre-filled edit/delete forms) has real, correctly-associated labels on
    every field, not just placeholder text."""
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user)
    event = await make_event(status=PublishStatus.PUBLISHED, name="Shows A11y Expanded Event")
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    await make_ticket_type(show_id=show.id, name="Adult")

    response = await axe_page.goto(f"/events/{event.id}/shows?open={show.id}")
    assert response is not None

    await axe_page.click('summary:has-text("Edit “Adult”")')
    await axe_page.click('summary:has-text("Add a ticket type")')
    # Both panels' fields are now in the accessibility tree as genuinely
    # visible/expanded content, not just present-but-collapsed DOM.
    await axe_page.wait_for_selector('#new-tt-name-' + str(show.id))

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


async def test_agent_accounts_list_page_has_no_axe_violations(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_agent_account: Callable[..., Awaitable[SeededAgent]],
) -> None:
    """Both real status branches on the same page: an active account (its
    "Revoke" form) and a revoked one (the plain "Revoked." text in place of
    a form) — the plain ``GET`` render, never carrying a freshly-created key
    (see the dedicated key-reveal test below for that branch)."""
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user)
    await make_agent_account(name="Active Agent")
    await make_agent_account(name="Revoked Agent", is_active=False)

    violations = await run_axe(axe_page, "/agent-accounts")

    assert violations == [], format_axe_violations(violations)


async def test_agent_accounts_list_page_with_no_accounts_has_no_axe_violations(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
) -> None:
    """No AgentAccount rows at all — the "No agent accounts yet." empty-state
    branch."""
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user)

    violations = await run_axe(axe_page, "/agent-accounts")

    assert violations == [], format_axe_violations(violations)


async def test_agent_accounts_list_page_with_freshly_revealed_key_has_no_axe_violations(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
) -> None:
    """The one-time API-key-reveal panel (``POST /agent-accounts`` renders
    the list page directly with the new key in context — see
    ``app.web.routes.agent_accounts``'s module docstring for why this can't
    use the usual redirect-with-flash pattern), reached via a real
    Playwright form submission rather than ``run_axe``'s navigate-first
    helper (same reasoning as the login error-state test above: this
    state can only be reached by actually submitting the form).

    Specifically exercises the ``role="alert"`` banner + ``aria-labelledby``
    section this milestone's accessibility-auditor pass reviewed for a
    "clear, immediate announcement that a new key was created and needs
    copying now" per this milestone's task, plus the "Copy API key" button's
    shared ``aria-live`` status-announcer region (``app/templates/backoffice/
    base.html``'s ``#bo-status-announcer``)."""
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user)

    await axe_page.goto("/agent-accounts")
    await axe_page.fill("#agent-name", "Freshly Created Agent")
    await axe_page.click('button[type="submit"]:has-text("Create agent account")')
    await axe_page.wait_for_selector("#new-agent-key-value")

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


async def test_audit_log_page_has_no_axe_violations(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    client_factory: Callable[[str | None], AsyncClient],
) -> None:
    """Real, distinctly-attributed entries: a human-created Event (the
    ``human`` actor badge, a real ``detail`` JSON blob rendered in the
    "Detail" column's ``<pre>``, and a real target UUID exercising the
    truncated-visible/full-text-for-assistive-tech ``target_id`` markup)
    and an agent-created Event (the ``ai_agent`` actor badge) — same seeding
    approach as ``tests/integration/test_web_audit_log_routes.py``'s
    attribution tests, reused here for the axe sweep rather than hand-
    crafting ``AuditLogEntry`` rows directly."""
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user)

    create_event = await client.post(
        "/api/v1/events", json={"name": "Audited A11y Event", "slug": f"audited-a11y-{uuid.uuid4().hex[:8]}"}
    )
    assert create_event.status_code == 201, create_event.text

    create_agent = await client.post("/api/v1/admin/agent-accounts", json={"name": "audit-a11y-agent"})
    assert create_agent.status_code == 201, create_agent.text
    raw_key = create_agent.json()["api_key"]
    async with client_factory(None) as agent_client:
        agent_event = await agent_client.post(
            "/api/v1/events",
            json={"name": "Agent Audited A11y Event", "slug": f"agent-audited-a11y-{uuid.uuid4().hex[:8]}"},
            headers={"X-Agent-Api-Key": raw_key},
        )
        assert agent_event.status_code == 201, agent_event.text

    violations = await run_axe(axe_page, "/audit-log")

    assert violations == [], format_axe_violations(violations)


async def test_audit_log_page_with_no_entries_has_no_axe_violations(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
) -> None:
    """No AuditLogEntry rows at all — the "No audit log entries yet."
    empty-state branch. Logging in itself writes no audit-log entry (login
    is not a backoffice mutation), so a freshly seeded admin with no other
    action taken reaches this branch directly."""
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user)

    violations = await run_axe(axe_page, "/audit-log")

    assert violations == [], format_axe_violations(violations)


async def test_admin_users_list_page_has_no_axe_violations(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
) -> None:
    """Three distinct per-row shapes in one table, in one pass — exactly
    what this milestone's accessibility-auditor task calls out as the
    tricky case: the logged-in admin's own row (no "Deactivate" action, a
    visible "You can't deactivate your own account." note instead, and its
    "Reset password" form WOULD include ``current_password`` if expanded),
    another active admin's row (has "Deactivate"), and an inactive admin's
    row ("Reactivate" in place of "Deactivate"). Confirms the row-to-row
    Actions-cell shape difference doesn't break the table's axe-visible
    structure (every row still has the same cell count — see this
    milestone's accessibility-auditor handoff for the manual screen-reader-
    traversal note this automated check can't fully replace)."""
    seeded = await make_admin_user(email="self-admin@example.test")
    login = await client.post(
        "/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password}
    )
    assert login.status_code == 200
    await apply_set_cookie_headers(axe_page.context, login.headers.get_list("set-cookie"))

    await make_admin_user(email="other-active-admin@example.test")
    await make_admin_user(email="other-inactive-admin@example.test", is_active=False)

    violations = await run_axe(axe_page, "/admin-users")

    assert violations == [], format_axe_violations(violations)


async def test_admin_users_list_page_with_no_accounts_has_no_axe_violations(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
) -> None:
    """The list always contains at least the logged-in admin's own row (an
    ``AdminUser`` must exist to authenticate at all) — this is therefore the
    "exactly one row, the caller's own" minimal-population branch rather
    than a true empty-``<tbody>`` state (unlike Orders/Stats/Events'
    genuinely-empty branches above), included here for the same "distinct
    render path" discipline."""
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user)

    violations = await run_axe(axe_page, "/admin-users")

    assert violations == [], format_axe_violations(violations)


async def test_admin_users_list_page_with_self_reset_password_form_expanded_has_no_axe_violations(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
) -> None:
    """Real interaction to expand the logged-in admin's own "Reset
    password" ``<details>`` panel — the one row whose reset-password form
    includes the extra ``current_password`` field (see ``app.web.routes.
    admin_users``'s module docstring). Confirms that field carries a real,
    correctly-associated ``<label>`` distinct from the ``new_password``
    field right next to it, not just placeholder text or implicit
    proximity."""
    seeded = await make_admin_user(email="expand-self-admin@example.test")
    login = await client.post(
        "/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password}
    )
    assert login.status_code == 200
    await apply_set_cookie_headers(axe_page.context, login.headers.get_list("set-cookie"))

    response = await axe_page.goto("/admin-users")
    assert response is not None
    await axe_page.click('summary:has-text("Reset password")')
    await axe_page.wait_for_selector(f"#current-password-{seeded.user.id}")

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

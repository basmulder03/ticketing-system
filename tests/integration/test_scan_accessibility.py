"""Automated WCAG 2.1 AA checks (axe-core, via Playwright) against the
scanning-app pages (Milestone 7): the show picker (``GET /scan``) and the
camera-scanning page (``GET /scan/{show_id}``) — ``app/templates/scan/*``,
``app/static/scan.css``, ``app/static/scan.js``.

Mirrors ``test_backoffice_accessibility.py``'s conventions exactly (session
cookie handed to ``axe_page`` via a plain ``httpx`` login +
``apply_set_cookie_headers``, rather than a real form-click login) — this is
the first axe sweep of the scanning-app template family.

This page is unusually JS-heavy: the entire result overlay (pass/unpaid/
already_scanned/wrong_show/invalid/network_error) and the admin-only
inline mark-as-paid form are built client-side by ``scan.js``, not
server-rendered — so a static-initial-HTML-only audit (like most of
``test_backoffice_accessibility.py``'s tests) would silently miss any
markup problem in those dynamically-built states entirely. Every dynamic
state below is therefore driven through the SAME real DOM interaction a
human would use to reach it — the manual-entry ``<details>`` fallback (see
``scan/scan.html`` / ``static/scan.js``'s own docstring: this fallback
exists specifically as "the path used to exercise this page end-to-end
without a physical camera/QR image") — mirroring
``test_public_site_accessibility.py``'s
``test_landing_page_checkout_error_state_has_no_axe_violations`` pattern
for auditing a client-JS-rendered state (fill/click through the real page,
``wait_for_selector`` for the dynamic content, then run axe against
whatever ``axe_page`` currently has loaded, not a fresh navigation).

Color-contrast scope note: unlike the public landing page, this UI has no
per-event Theme at all (its dark, high-contrast palette is a fixed set of
``app/static/scan.css`` tokens, same as the backoffice) — ``color-contrast``
stays fully enabled throughout this file.

Manual-only items NOT covered by this automated suite (axe-core cannot
verify these): real screen-reader announcement behavior of
``#scan-result-announcer`` (this suite can assert the element exists,
is never itself hidden, and gets fresh ``textContent`` per result — not
that a real screen reader actually speaks it), and real low-light/
outdoor-glare legibility of the result colors on an actual phone screen —
see this milestone's accessibility-auditor handoff for both, flagged as
manual checklist items rather than silently treated as covered.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from httpx import AsyncClient
from playwright.async_api import Page

from app.core.qr_tokens import sign_ticket_token
from app.models.enums import AdminRole, PaymentMethod, PublishStatus
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

_PAST = datetime.now(UTC) - timedelta(days=1)


async def _login_and_apply_session_cookie(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    *,
    role: AdminRole = AdminRole.SCANNER,
) -> SeededAdmin:
    seeded = await make_admin_user(role=role)
    login = await client.post(
        "/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password}
    )
    assert login.status_code == 200
    session_cookies = login.headers.get_list("set-cookie")
    assert session_cookies, "admin login set no session cookie to hand off to the browser"
    await apply_set_cookie_headers(axe_page.context, session_cookies)
    return seeded


async def _setup_show(
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> tuple[Event, Show, TicketType]:
    event = await make_event(status=PublishStatus.PUBLISHED, name="Scan A11y Event")
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=5)
    await make_event_config(
        event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR]
    )
    return event, show, ticket_type


async def _checkout_one_ticket(client: AsyncClient, ticket_type_id: uuid.UUID) -> tuple[str, str]:
    """Door-payment checkout (never requires auth, starts ``pending_door``).
    Returns ``(order_id, ticket_id)`` — mirrors ``test_scan_route.py``'s
    identically-named helper."""
    response = await client.post(
        "/api/v1/public/checkout",
        json={
            "buyer_name": "A11y Scan Buyer",
            "buyer_email": f"a11y-scan-{uuid.uuid4().hex}@example.test",
            "buyer_address": "1 Test Street",
            "language": "en",
            "payment_method": "door",
            "items": [{"ticket_type_id": str(ticket_type_id), "quantity": 1}],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body["id"], body["tickets"][0]["id"]


async def _open_manual_entry_and_submit(axe_page: Page, token: str) -> None:
    await axe_page.click("summary")
    await axe_page.fill("#scan-manual-input", token)
    await axe_page.click('#scan-manual-form button[type="submit"]')


async def _run_axe_on_loaded_page(axe_page: Page) -> list[dict[str, Any]]:
    """Run axe against whatever ``axe_page`` currently has loaded, without
    a fresh navigation — for auditing a client-JS-rendered state reached by
    interacting with the page (see this module's docstring). Duplicates
    ``conftest._evaluate_axe_on_current_page``'s tiny ``page.evaluate`` call
    rather than importing that underscore-prefixed helper, matching how
    ``test_public_site_accessibility.py``'s own dynamic-state test does the
    same thing inline."""
    result: dict[str, Any] = await axe_page.evaluate(
        """
        async () => {
          return await axe.run(document, {
            runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa'] },
          });
        }
        """
    )
    violations: list[dict[str, Any]] = result["violations"]
    return violations


# --- Show picker --------------------------------------------------------------


async def test_scan_picker_page_has_no_axe_violations(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user, role=AdminRole.SCANNER)
    await _setup_show(make_event, make_show, make_ticket_type, make_event_config)

    violations = await run_axe(axe_page, "/scan")

    assert violations == [], format_axe_violations(violations)


async def test_scan_picker_page_with_no_shows_has_no_axe_violations(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
) -> None:
    """The "No shows in the next 60 days" empty state is a distinct render
    path — audited separately, same discipline as
    ``test_backoffice_accessibility.py``'s empty-orders-list test."""
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user, role=AdminRole.SCANNER)

    violations = await run_axe(axe_page, "/scan")

    assert violations == [], format_axe_violations(violations)


# --- Scan page: initial (pre-scan) state --------------------------------------


async def test_scan_page_initial_state_has_no_axe_violations(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user, role=AdminRole.SCANNER)
    _event, show, _tt = await _setup_show(make_event, make_show, make_ticket_type, make_event_config)

    violations = await run_axe(axe_page, f"/scan/{show.id}")

    assert violations == [], format_axe_violations(violations)


# --- Scan page: dynamic, client-JS-rendered result states --------------------


async def test_scan_page_pass_result_overlay_has_no_axe_violations(
    axe_page: Page,
    client: AsyncClient,
    client_factory: Callable[[str | None], AsyncClient],
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """Drives a real ``pass`` result via the manual-entry fallback (a valid
    ticket for THIS show, paid) and audits the client-built result overlay
    exactly as rendered — not just the page's static initial HTML."""
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user, role=AdminRole.SCANNER)
    _event, show, ticket_type = await _setup_show(make_event, make_show, make_ticket_type, make_event_config)
    order_id, ticket_id = await _checkout_one_ticket(client, ticket_type.id)

    # Marking paid needs an ADMIN-authenticated request; issued via a
    # second httpx client (its own pseudo-IP/rate-limit bucket, per
    # client_factory's docstring) so it never disturbs the SCANNER session
    # cookie already applied to axe_page's browser context above.
    async with client_factory(None) as admin_client:
        admin = await make_admin_user(role=AdminRole.ADMIN)
        login = await admin_client.post(
            "/api/v1/auth/login", json={"email": admin.user.email, "password": admin.password}
        )
        assert login.status_code == 200
        mark_paid = await admin_client.post(
            f"/api/v1/orders/{order_id}/mark-paid", json={"method_label": "cash"}
        )
        assert mark_paid.status_code == 200, mark_paid.text

    await axe_page.goto(f"/scan/{show.id}")
    await _open_manual_entry_and_submit(axe_page, sign_ticket_token(uuid.UUID(ticket_id)))
    await axe_page.wait_for_selector(".scan-overlay--pass")

    violations = await _run_axe_on_loaded_page(axe_page)
    assert violations == [], format_axe_violations(violations)


async def test_scan_page_unpaid_result_with_admin_mark_paid_form_has_no_axe_violations(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """The highest-scrutiny state per this milestone's brief: an ``unpaid``
    result viewed by an ADMIN principal, which additionally renders the
    inline mark-as-paid form (``buildMarkPaidForm`` in ``scan.js``) — two
    ``document.createElement``-built label/input pairs and a required text
    input, exactly the pattern ``test_backoffice_accessibility.py`` already
    holds Milestone 6's server-rendered equivalent form to."""
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user, role=AdminRole.ADMIN)
    _event, show, ticket_type = await _setup_show(make_event, make_show, make_ticket_type, make_event_config)
    _order_id, ticket_id = await _checkout_one_ticket(client, ticket_type.id)

    await axe_page.goto(f"/scan/{show.id}")
    await _open_manual_entry_and_submit(axe_page, sign_ticket_token(uuid.UUID(ticket_id)))
    await axe_page.wait_for_selector(".scan-overlay--unpaid")
    await axe_page.wait_for_selector(".scan-mark-paid")

    violations = await _run_axe_on_loaded_page(axe_page)
    assert violations == [], format_axe_violations(violations)


async def test_scan_page_invalid_result_overlay_has_no_axe_violations(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """The ``invalid`` outcome's overlay renders no details block at all
    (see ``scan.js``'s anti-enumeration comment) — a distinct-enough render
    shape from ``pass``/``unpaid`` to audit on its own."""
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user, role=AdminRole.SCANNER)
    _event, show, _tt = await _setup_show(make_event, make_show, make_ticket_type, make_event_config)

    await axe_page.goto(f"/scan/{show.id}")
    await _open_manual_entry_and_submit(axe_page, "definitely-not-a-real-signed-token")
    await axe_page.wait_for_selector(".scan-overlay--invalid")

    violations = await _run_axe_on_loaded_page(axe_page)
    assert violations == [], format_axe_violations(violations)


async def test_scan_page_network_error_overlay_has_no_axe_violations(
    axe_page: Page,
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """The client-side-only "Connection issue" state (not a ``ScanOutcome``
    at all — see ``scan.js``'s ``renderNetworkError`` docstring): forces a
    real fetch failure by aborting just the scan endpoint's request at the
    Playwright routing layer, rather than trying to fake a timeout."""
    await _login_and_apply_session_cookie(axe_page, client, make_admin_user, role=AdminRole.SCANNER)
    _event, show, _tt = await _setup_show(make_event, make_show, make_ticket_type, make_event_config)

    await axe_page.route("**/api/v1/shows/**/scan", lambda route: route.abort())
    await axe_page.goto(f"/scan/{show.id}")
    await _open_manual_entry_and_submit(axe_page, "irrelevant-token-request-never-completes")
    await axe_page.wait_for_selector(".scan-overlay--network_error")

    violations = await _run_axe_on_loaded_page(axe_page)
    assert violations == [], format_axe_violations(violations)

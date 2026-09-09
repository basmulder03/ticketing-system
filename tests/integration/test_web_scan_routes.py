"""Integration tests for the scanning-app HTML pages (Milestone 7):
``GET /scan`` (show picker) and ``GET /scan/{show_id}`` (camera page) —
``app/web/routes/scan.py``, gated by ``require_web_scanner_or_admin``
(``app/web/deps.py``).

Mirrors ``tests/integration/test_web_orders_routes.py``/
``test_web_backoffice_routes.py``'s conventions: a local ``_api_login``
helper (bypasses the web login form/CSRF for tests whose focus is
something other than the login flow itself), per-page unauthenticated
-redirect tests rather than a shared scoping table (this project's web
layer has no such table — see those files' module docstrings).

The scanner-role login-default-redirect behavior
(``app.web.routes.auth._apply_role_default``) and ``mark_order_paid_web``'s
``return_to`` open-redirect guard are covered in
``test_web_backoffice_routes.py``/``test_web_orders_routes.py``
respectively, not here — this file is scoped to the two scanning-app pages
themselves.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient

from app.models.enums import AdminRole, PaymentMethod, PublishStatus
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.show import Show
from app.models.ticket_type import TicketType
from tests.integration.conftest import SeededAdmin

_PAST = datetime.now(UTC) - timedelta(days=1)


async def _api_login(client: AsyncClient, seeded: SeededAdmin) -> None:
    response = await client.post(
        "/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password}
    )
    assert response.status_code == 200


async def _setup_show(
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> tuple[Event, Show]:
    event = await make_event(status=PublishStatus.PUBLISHED, name="Scan Web Event")
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    await make_ticket_type(show_id=show.id)
    await make_event_config(
        event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR]
    )
    return event, show


# --- Unauthenticated access --------------------------------------------------


async def test_unauthenticated_picker_redirects_to_login(client: AsyncClient) -> None:
    response = await client.get("/scan")

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


async def test_unauthenticated_scan_page_redirects_to_login(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
) -> None:
    event = await make_event()
    show = await make_show(event_id=event.id)

    response = await client.get(f"/scan/{show.id}")

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


# --- Scanner-role and admin-role can both reach both pages -----------------


async def test_scanner_role_can_reach_picker_and_scan_page(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    await _api_login(client, await make_admin_user(role=AdminRole.SCANNER))
    event, show = await _setup_show(make_event, make_show, make_ticket_type, make_event_config)

    picker_response = await client.get("/scan")
    scan_response = await client.get(f"/scan/{show.id}")

    assert picker_response.status_code == 200
    assert scan_response.status_code == 200
    assert "Scan Web Event" in scan_response.text


async def test_admin_role_can_reach_picker_and_scan_page(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    await _api_login(client, await make_admin_user(role=AdminRole.ADMIN))
    event, show = await _setup_show(make_event, make_show, make_ticket_type, make_event_config)

    picker_response = await client.get("/scan")
    scan_response = await client.get(f"/scan/{show.id}")

    assert picker_response.status_code == 200
    assert scan_response.status_code == 200


# --- 404 for an unknown show id ----------------------------------------------


async def test_scan_page_404s_for_unknown_show_id(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user(role=AdminRole.SCANNER))

    response = await client.get(f"/scan/{uuid.uuid4()}")

    assert response.status_code == 404


async def test_scan_page_404s_for_malformed_show_id(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user(role=AdminRole.SCANNER))

    response = await client.get("/scan/not-a-valid-uuid")

    assert response.status_code == 404


# --- isAdmin flag in the client-side config (drives the mark-as-paid form) --


async def test_scan_page_is_admin_flag_is_true_for_admin_role(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    await _api_login(client, await make_admin_user(role=AdminRole.ADMIN))
    event, show = await _setup_show(make_event, make_show, make_ticket_type, make_event_config)

    response = await client.get(f"/scan/{show.id}")

    assert response.status_code == 200
    assert "isAdmin: true" in response.text


async def test_scan_page_is_admin_flag_is_false_for_scanner_role(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    await _api_login(client, await make_admin_user(role=AdminRole.SCANNER))
    event, show = await _setup_show(make_event, make_show, make_ticket_type, make_event_config)

    response = await client.get(f"/scan/{show.id}")

    assert response.status_code == 200
    assert "isAdmin: false" in response.text


# --- Picker lists near-term shows --------------------------------------------


async def test_picker_lists_a_near_term_show(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    await _api_login(client, await make_admin_user(role=AdminRole.SCANNER))
    event, show = await _setup_show(make_event, make_show, make_ticket_type, make_event_config)

    response = await client.get("/scan")

    assert response.status_code == 200
    assert f'href="/scan/{show.id}"' in response.text
    assert "Scan Web Event" in response.text

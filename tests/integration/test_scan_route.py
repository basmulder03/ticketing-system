"""Integration tests for the door-scanning API (Milestone 7):
``POST /api/v1/shows/{show_id}/scan`` (``app.services.scan.scan_ticket`` /
``app.api.routes.scan``) and the scanner-facing show lookup routes
(``app.api.routes.scan_shows``).

Mirrors ``tests/integration/test_mark_order_paid_route.py``'s house style
(setup helpers, real HTTP requests via ``client``/``client_factory``, the
shared ``make_event``/``make_show``/``make_ticket_type``/``make_event_config``/
``make_admin_user`` fixtures) and ``tests/integration/test_order_payment_
concurrency.py``'s concurrency pattern (``asyncio.gather`` over a fresh
per-iteration client).

A scanned QR token only needs to be a validly HMAC-signed encoding of a
real ``Ticket.id`` (see ``app.core.qr_tokens``/``app.services.scan``'s
module docstring) — ``scan_ticket`` never compares the presented token
against ``Ticket.qr_token`` in the DB, so every test here mints a fresh
token directly via ``sign_ticket_token(ticket_id)`` rather than reading
back whatever ``mark-paid`` happened to persist.

Covers, per ``app.services.scan``'s documented outcome ordering: pass,
already_scanned, wrong_show, invalid (both sub-cases, proving the
anti-enumeration identical-message property), unpaid (``pending_door`` and
a second non-paid status), the safety-critical concurrency guarantee, 404
for a malformed/unknown ``show_id``, and admin/scanner/agent/unauthenticated
scoping across all three ``require_scanner_or_admin``-gated routes (POST
scan + the two GET scan/shows routes) — deliberately NOT added to
``test_deps_admin_scoping.py``'s ``ADMIN_GATED_ROUTES`` table, since that
table is specifically for ``require_admin``-only routes where a
scanner-role human must be REJECTED; these routes must instead ADMIT a
scanner-role human, the opposite assertion.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.qr_tokens import sign_ticket_token
from app.models.audit_log import AuditLogEntry
from app.models.enums import AdminRole, OrderStatus, PaymentMethod, PublishStatus
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.order import Order
from app.models.show import Show
from app.models.ticket import Ticket
from app.models.ticket_type import TicketType
from tests.integration.conftest import SeededAdmin, SeededAgent

_PAST = datetime.now(UTC) - timedelta(days=1)
_SCAN_ACTION = "ticket.scan"
_GENERIC_INVALID_TOKEN = "definitely-not-a-real-signed-token"


# --- Shared setup helpers ----------------------------------------------------


async def _setup_show(
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
    *,
    quantity_available: int = 5,
) -> tuple[Event, Show, TicketType]:
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=quantity_available)
    await make_event_config(
        event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR]
    )
    return event, show, ticket_type


async def _checkout_one_ticket(
    client: AsyncClient, ticket_type_id: uuid.UUID, *, buyer_email: str | None = None
) -> tuple[str, str]:
    """Checks out one ``door``-payment ticket (starts life ``pending_door``,
    never requires authentication). Returns ``(order_id, ticket_id)``."""
    response = await client.post(
        "/api/v1/public/checkout",
        json={
            "buyer_name": "Scan Test Buyer",
            "buyer_email": buyer_email or f"scan-{uuid.uuid4().hex}@example.test",
            "buyer_address": "1 Test Street",
            "language": "en",
            "payment_method": "door",
            "items": [{"ticket_type_id": str(ticket_type_id), "quantity": 1}],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body["id"], body["tickets"][0]["id"]


async def _login_as(client: AsyncClient, seeded: SeededAdmin) -> None:
    response = await client.post(
        "/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password}
    )
    assert response.status_code == 200


async def _mark_paid_as_admin(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], order_id: str
) -> SeededAdmin:
    """Logs the given client in as a fresh ADMIN and marks ``order_id`` paid
    (also proving ADMIN is a valid scanning principal, per
    ``require_scanner_or_admin``, so the same session can go on to scan)."""
    admin = await make_admin_user(role=AdminRole.ADMIN)
    await _login_as(client, admin)
    response = await client.post(f"/api/v1/orders/{order_id}/mark-paid", json={"method_label": "cash"})
    assert response.status_code == 200, response.text
    return admin


async def _paid_ticket(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> tuple[Event, Show, str, str, SeededAdmin]:
    """Full happy-path setup: a real paid Order with one Ticket, and
    ``client`` left authenticated as the ADMIN who marked it paid. Returns
    ``(event, show, order_id, ticket_id, admin)``."""
    event, show, ticket_type = await _setup_show(make_event, make_show, make_ticket_type, make_event_config)
    order_id, ticket_id = await _checkout_one_ticket(client, ticket_type.id)
    admin = await _mark_paid_as_admin(client, make_admin_user, order_id)
    return event, show, order_id, ticket_id, admin


async def _scan(client: AsyncClient, show_id: uuid.UUID | str, token: str) -> tuple[int, dict[str, Any]]:
    response = await client.post(f"/api/v1/shows/{show_id}/scan", json={"token": token})
    return response.status_code, response.json()


async def _scan_audit_entries(db_session: AsyncSession, ticket_id: str) -> list[AuditLogEntry]:
    result = await db_session.execute(
        select(AuditLogEntry).where(AuditLogEntry.action == _SCAN_ACTION).where(AuditLogEntry.target_id == ticket_id)
    )
    return list(result.scalars().all())


async def _ticket_by_id(db_session: AsyncSession, ticket_id: str) -> Ticket:
    result = await db_session.execute(select(Ticket).where(Ticket.id == uuid.UUID(ticket_id)))
    return result.scalar_one()


async def _order_by_id(db_session: AsyncSession, order_id: str) -> Order:
    result = await db_session.execute(select(Order).where(Order.id == uuid.UUID(order_id)))
    return result.scalar_one()


# --- pass ---------------------------------------------------------------------


async def test_pass_scans_and_completes_entry_with_one_audit_entry(
    client: AsyncClient,
    db_session: AsyncSession,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    _event, show, order_id, ticket_id, admin = await _paid_ticket(
        client, make_admin_user, make_event, make_show, make_ticket_type, make_event_config
    )
    token = sign_ticket_token(uuid.UUID(ticket_id))

    status_code, body = await _scan(client, show.id, token)

    assert status_code == 200
    assert body["outcome"] == "pass"
    assert body["ticket_id"] == ticket_id
    assert body["ticket_type_name"] == "Adult"
    assert body["buyer_name"] == "Scan Test Buyer"
    assert body["order_id"] == order_id
    assert body["order_status"] == "paid"
    assert body["scanned_at"] is not None
    assert body["scanned_by_name"] == admin.user.email

    ticket = await _ticket_by_id(db_session, ticket_id)
    assert ticket.scanned_at is not None
    assert ticket.scanned_by == admin.user.id

    entries = await _scan_audit_entries(db_session, ticket_id)
    assert len(entries) == 1
    assert entries[0].detail is not None
    assert entries[0].detail["result"] == "pass"
    assert entries[0].actor_id == admin.user.id


# --- already_scanned ------------------------------------------------------------


async def test_already_scanned_on_repeat_scan_does_not_remutate_the_ticket(
    client: AsyncClient,
    db_session: AsyncSession,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    _event, show, _order_id, ticket_id, admin = await _paid_ticket(
        client, make_admin_user, make_event, make_show, make_ticket_type, make_event_config
    )
    token = sign_ticket_token(uuid.UUID(ticket_id))

    first_status, first_body = await _scan(client, show.id, token)
    assert first_status == 200 and first_body["outcome"] == "pass"

    second_status, second_body = await _scan(client, show.id, token)

    assert second_status == 200
    assert second_body["outcome"] == "already_scanned"
    assert second_body["ticket_id"] == ticket_id
    assert second_body["scanned_at"] == first_body["scanned_at"]
    assert second_body["scanned_by_name"] == admin.user.email

    ticket = await _ticket_by_id(db_session, ticket_id)
    assert ticket.scanned_at is not None
    # Compare as parsed datetimes rather than raw strings: pydantic's JSON
    # encoding uses a "Z" UTC suffix while Python's own `isoformat()` uses
    # "+00:00" for the same instant.
    assert ticket.scanned_at == datetime.fromisoformat(first_body["scanned_at"])
    assert ticket.scanned_at == datetime.fromisoformat(second_body["scanned_at"])

    entries = await _scan_audit_entries(db_session, ticket_id)
    results = sorted(str(e.detail["result"]) for e in entries if e.detail)
    assert results == ["already_scanned", "pass"], "exactly one pass mutation ever happened"


# --- wrong_show -----------------------------------------------------------------


async def test_wrong_show_reports_the_tickets_actual_show_and_leaves_it_unscanned(
    client: AsyncClient,
    db_session: AsyncSession,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event, show, _order_id, ticket_id, _admin = await _paid_ticket(
        client, make_admin_user, make_event, make_show, make_ticket_type, make_event_config
    )
    other_show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    token = sign_ticket_token(uuid.UUID(ticket_id))

    status_code, body = await _scan(client, other_show.id, token)

    assert status_code == 200
    assert body["outcome"] == "wrong_show"
    assert body["ticket_id"] == ticket_id
    assert body["actual_show_id"] == str(show.id)
    assert body["actual_show_label"] == f"{event.name} — {show.date.isoformat()}"

    ticket = await _ticket_by_id(db_session, ticket_id)
    assert ticket.scanned_at is None

    entries = await _scan_audit_entries(db_session, ticket_id)
    assert len(entries) == 1
    assert entries[0].detail is not None
    assert entries[0].detail["result"] == "wrong_show"


# --- invalid: anti-enumeration identical message ---------------------------------


async def test_invalid_garbage_and_unknown_ticket_id_produce_identical_generic_response(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    _event, show, _ticket_type = await _setup_show(make_event, make_show, make_ticket_type, make_event_config)
    admin = await make_admin_user(role=AdminRole.ADMIN)
    await _login_as(client, admin)

    garbage_status, garbage_body = await _scan(client, show.id, _GENERIC_INVALID_TOKEN)
    unknown_token = sign_ticket_token(uuid.uuid4())
    unknown_status, unknown_body = await _scan(client, show.id, unknown_token)

    assert garbage_status == 200
    assert unknown_status == 200
    assert garbage_body["outcome"] == "invalid"
    assert unknown_body["outcome"] == "invalid"
    assert garbage_body["ticket_id"] is None
    assert unknown_body["ticket_id"] is None
    # THE anti-enumeration property: a tampered/garbage signature must be
    # indistinguishable from a well-formed token naming an unknown ticket.
    assert garbage_body["message"] == unknown_body["message"]


# --- unpaid -----------------------------------------------------------------------


async def test_unpaid_pending_door_order_does_not_complete_entry(
    client: AsyncClient,
    db_session: AsyncSession,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    _event, show, ticket_type = await _setup_show(make_event, make_show, make_ticket_type, make_event_config)
    admin = await make_admin_user(role=AdminRole.ADMIN)
    await _login_as(client, admin)
    order_id, ticket_id = await _checkout_one_ticket(client, ticket_type.id)

    order = await _order_by_id(db_session, order_id)
    assert order.status == OrderStatus.PENDING_DOOR

    token = sign_ticket_token(uuid.UUID(ticket_id))
    status_code, body = await _scan(client, show.id, token)

    assert status_code == 200
    assert body["outcome"] == "unpaid"
    assert body["ticket_id"] == ticket_id
    assert body["order_id"] == order_id
    assert body["order_status"] == "pending_door"
    assert Decimal(str(body["amount_due"])) == order.total

    ticket = await _ticket_by_id(db_session, ticket_id)
    assert ticket.scanned_at is None, "an unpaid scan must never complete entry"


@pytest.mark.parametrize("other_status", [OrderStatus.PENDING, OrderStatus.CANCELLED])
async def test_unpaid_is_not_special_cased_to_pending_door_only(
    other_status: OrderStatus,
    client: AsyncClient,
    db_session: AsyncSession,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """``app.services.scan.scan_ticket`` treats ANY non-``paid`` status as
    ``unpaid``, not just ``pending_door`` — force the Order into a
    different non-paid status directly and confirm the same outcome."""
    _event, show, ticket_type = await _setup_show(make_event, make_show, make_ticket_type, make_event_config)
    admin = await make_admin_user(role=AdminRole.ADMIN)
    await _login_as(client, admin)
    order_id, ticket_id = await _checkout_one_ticket(client, ticket_type.id)

    order = await _order_by_id(db_session, order_id)
    order.status = other_status
    await db_session.commit()

    token = sign_ticket_token(uuid.UUID(ticket_id))
    status_code, body = await _scan(client, show.id, token)

    assert status_code == 200
    assert body["outcome"] == "unpaid"
    assert body["order_status"] == other_status.value

    ticket = await _ticket_by_id(db_session, ticket_id)
    assert ticket.scanned_at is None


# --- scanner-role can actually perform a real pass scan -------------------------


async def test_scanner_role_principal_can_perform_a_real_pass_scan(
    client: AsyncClient,
    client_factory: Callable[[str | None], AsyncClient],
    db_session: AsyncSession,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    _event, show, ticket_type = await _setup_show(make_event, make_show, make_ticket_type, make_event_config)
    async with client_factory(None) as setup_client:
        order_id, ticket_id = await _checkout_one_ticket(setup_client, ticket_type.id)
        await _mark_paid_as_admin(setup_client, make_admin_user, order_id)

    scanner = await make_admin_user(role=AdminRole.SCANNER)
    await _login_as(client, scanner)
    token = sign_ticket_token(uuid.UUID(ticket_id))

    status_code, body = await _scan(client, show.id, token)

    assert status_code == 200
    assert body["outcome"] == "pass"
    assert body["scanned_by_name"] == scanner.user.email

    ticket = await _ticket_by_id(db_session, ticket_id)
    assert ticket.scanned_by == scanner.user.id


# --- Concurrency: THE safety-critical test --------------------------------------

_CONCURRENT_SCANS = 8
_SCAN_ITERATIONS = 3


async def test_concurrent_scans_of_the_same_paid_ticket_yield_exactly_one_pass(
    db_session: AsyncSession,
    client_factory: Callable[[str | None], AsyncClient],
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """Fires genuinely concurrent (``asyncio.gather``) scan requests for the
    SAME valid, paid ticket's token against the same show. Exactly one may
    come back ``pass``; the rest must come back ``already_scanned`` — the
    row-locked ``SELECT ... FOR UPDATE`` in ``app.services.scan.scan_ticket``
    is what this asserts is actually closing the race, at the real HTTP
    request-handling layer (mirrors ``test_order_payment_concurrency.py``'s
    structure), not just in isolation.
    """
    for iteration in range(_SCAN_ITERATIONS):
        _event, show, ticket_type = await _setup_show(make_event, make_show, make_ticket_type, make_event_config)

        async with client_factory(f"scan-race-{iteration}-{uuid.uuid4().hex[:8]}") as client:
            order_id, ticket_id = await _checkout_one_ticket(
                client, ticket_type.id, buyer_email=f"scan-race-{iteration}-{uuid.uuid4().hex}@example.test"
            )
            await _mark_paid_as_admin(client, make_admin_user, order_id)
            token = sign_ticket_token(uuid.UUID(ticket_id))

            responses = await asyncio.gather(
                *[
                    client.post(f"/api/v1/shows/{show.id}/scan", json={"token": token})
                    for _ in range(_CONCURRENT_SCANS)
                ]
            )

        assert all(r.status_code == 200 for r in responses), (
            f"iteration {iteration}: expected all 200s, got statuses={sorted(r.status_code for r in responses)}"
        )
        outcomes = [r.json()["outcome"] for r in responses]
        passes = [o for o in outcomes if o == "pass"]
        already_scanned = [o for o in outcomes if o == "already_scanned"]

        assert len(passes) == 1, f"iteration {iteration}: expected exactly 1 pass, got outcomes={sorted(outcomes)}"
        assert len(already_scanned) == _CONCURRENT_SCANS - 1, (
            f"iteration {iteration}: expected the rest to be already_scanned, got outcomes={sorted(outcomes)}"
        )

        ticket = await _ticket_by_id(db_session, ticket_id)
        assert ticket.scanned_at is not None

        entries = await _scan_audit_entries(db_session, ticket_id)
        pass_entries = [e for e in entries if e.detail and e.detail.get("result") == "pass"]
        assert len(pass_entries) == 1, (
            f"iteration {iteration}: expected exactly ONE ticket.scan pass audit entry despite "
            f"{_CONCURRENT_SCANS} concurrent duplicate scans, got {len(pass_entries)}"
        )


# --- 404 --------------------------------------------------------------------------


async def test_scan_returns_404_for_malformed_show_id(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    admin = await make_admin_user(role=AdminRole.ADMIN)
    await _login_as(client, admin)

    response = await client.post("/api/v1/shows/not-a-valid-uuid/scan", json={"token": _GENERIC_INVALID_TOKEN})

    assert response.status_code == 404


async def test_scan_returns_404_for_unknown_show_id(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    admin = await make_admin_user(role=AdminRole.ADMIN)
    await _login_as(client, admin)

    response = await client.post(
        f"/api/v1/shows/{uuid.uuid4()}/scan", json={"token": _GENERIC_INVALID_TOKEN}
    )

    assert response.status_code == 404


# --- Scoping: admin/scanner pass, agent/unauthenticated rejected -----------------
#
# Deliberately NOT added to test_deps_admin_scoping.py's ADMIN_GATED_ROUTES
# table (that table asserts a scanner-role human is REJECTED — the opposite
# of what require_scanner_or_admin must do). Every request below sends a
# harmless placeholder body/id: what's asserted is purely the auth-layer
# status code (200/404 = passed auth and reached the route's own logic;
# 401/403 = rejected by auth), never the scan/show-lookup outcome itself.

_PLACEHOLDER_SHOW_ID = "44444444-4444-4444-4444-444444444444"

# (label, method, path, json_body)
SCANNER_GATED_ROUTES: list[tuple[str, str, str, dict[str, str] | None]] = [
    (
        "scan_ticket",
        "POST",
        f"/api/v1/shows/{_PLACEHOLDER_SHOW_ID}/scan",
        {"token": "irrelevant-scoping-probe"},
    ),
    ("list_scannable_shows", "GET", "/api/v1/scan/shows", None),
    ("get_scannable_show", "GET", f"/api/v1/scan/shows/{_PLACEHOLDER_SHOW_ID}", None),
]

_IDS = [route[0] for route in SCANNER_GATED_ROUTES]


async def _request(client: AsyncClient, method: str, path: str, json_body: dict[str, str] | None) -> int:
    response = await client.request(method, path, json=json_body)
    return response.status_code


@pytest.mark.parametrize(("label", "method", "path", "json_body"), SCANNER_GATED_ROUTES, ids=_IDS)
async def test_real_admin_can_reach_scanner_gated_routes(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    label: str,
    method: str,
    path: str,
    json_body: dict[str, str] | None,
) -> None:
    seeded = await make_admin_user(role=AdminRole.ADMIN)
    await _login_as(client, seeded)

    status_code = await _request(client, method, path, json_body)

    assert status_code not in (401, 403)


@pytest.mark.parametrize(("label", "method", "path", "json_body"), SCANNER_GATED_ROUTES, ids=_IDS)
async def test_scanner_role_admin_can_reach_scanner_gated_routes(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    label: str,
    method: str,
    path: str,
    json_body: dict[str, str] | None,
) -> None:
    seeded = await make_admin_user(role=AdminRole.SCANNER)
    await _login_as(client, seeded)

    status_code = await _request(client, method, path, json_body)

    assert status_code not in (401, 403)


@pytest.mark.parametrize(("label", "method", "path", "json_body"), SCANNER_GATED_ROUTES, ids=_IDS)
async def test_agent_key_cannot_reach_scanner_gated_routes(
    client: AsyncClient,
    make_agent_account: Callable[..., Awaitable[SeededAgent]],
    label: str,
    method: str,
    path: str,
    json_body: dict[str, str] | None,
) -> None:
    seeded = await make_agent_account()
    client.headers["X-Agent-Api-Key"] = seeded.raw_key

    status_code = await _request(client, method, path, json_body)

    assert status_code == 403


@pytest.mark.parametrize(("label", "method", "path", "json_body"), SCANNER_GATED_ROUTES, ids=_IDS)
async def test_unauthenticated_caller_cannot_reach_scanner_gated_routes(
    client: AsyncClient, label: str, method: str, path: str, json_body: dict[str, str] | None
) -> None:
    status_code = await _request(client, method, path, json_body)

    assert status_code == 401

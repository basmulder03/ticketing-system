"""Integration tests for ``GET /api/v1/orders/{order_id}/invoice.pdf``
(``app.api.routes.orders.download_invoice_pdf``, Milestone 5).

Admin-only scoping (agent/scanner/unauthenticated all rejected) is covered
by the shared table in ``tests/integration/test_deps_admin_scoping.py`` —
not repeated here. This file covers the route's own behavior: 404 for an
unknown order id, the actual (not guessed) status for an order with no
invoice yet, and a successful download returning a real PDF for a paid
order.
"""

import uuid
from collections.abc import Awaitable, Callable

from httpx import AsyncClient

from app.models.enums import PaymentMethod, PublishStatus
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.show import Show
from app.models.ticket_type import TicketType
from tests.integration.conftest import SeededAdmin


async def _login_admin(client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]) -> None:
    seeded = await make_admin_user()
    login = await client.post(
        "/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password}
    )
    assert login.status_code == 200


async def _checkout_door_order(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> str:
    """Create a real, published Event/Show/TicketType and check out a
    ``payment_method=door`` order (still ``pending`` — Milestone 6's
    mark-as-paid isn't wired yet, so this is the simplest way to get a
    genuinely unpaid, invoice-less Order). Returns the order id."""
    from datetime import UTC, datetime, timedelta

    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=10)
    await make_event_config(
        event_id=event.id,
        sales_live_at=datetime.now(UTC) - timedelta(days=1),
        enabled_payment_methods=[PaymentMethod.DOOR],
    )

    response = await client.post(
        "/api/v1/public/checkout",
        json={
            "buyer_name": "Door Buyer",
            "buyer_email": f"door-{uuid.uuid4().hex}@example.test",
            "buyer_address": "1 Test Street",
            "language": "en",
            "payment_method": "door",
            "items": [{"ticket_type_id": str(ticket_type.id), "quantity": 1}],
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _checkout_simulated_paid_order(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> str:
    """Create a draft Event and check out via its preview token with
    ``payment_method=mollie`` and no Mollie key configured — takes the
    preview-mode simulated-payment path (see
    ``app.services.checkout._initiate_mollie_payment``), which leaves the
    Order genuinely ``paid`` with its Invoice already issued, with no
    Mollie mocking needed at all. Returns the order id."""
    event = await make_event(status=PublishStatus.DRAFT)
    show = await make_show(event_id=event.id, status=PublishStatus.DRAFT)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=10)
    await make_event_config(event_id=event.id, enabled_payment_methods=[PaymentMethod.MOLLIE])

    response = await client.post(
        "/api/v1/public/checkout",
        json={
            "buyer_name": "Preview Buyer",
            "buyer_email": f"preview-{uuid.uuid4().hex}@example.test",
            "buyer_address": "1 Test Street",
            "language": "en",
            "payment_method": "mollie",
            "items": [{"ticket_type_id": str(ticket_type.id), "quantity": 1}],
            "preview_token": event.preview_token,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "paid"
    return str(body["id"])


async def test_returns_404_for_unknown_order_id(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login_admin(client, make_admin_user)

    response = await client.get(f"/api/v1/orders/{uuid.uuid4()}/invoice.pdf")

    assert response.status_code == 404


async def test_returns_404_for_an_order_with_no_invoice_yet(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """A still-pending (door, never reconciled) order has no Invoice —
    confirms the ACTUAL documented behavior (404, not e.g. 409) rather than
    assuming it."""
    await _login_admin(client, make_admin_user)
    order_id = await _checkout_door_order(client, make_event, make_show, make_ticket_type, make_event_config)

    response = await client.get(f"/api/v1/orders/{order_id}/invoice.pdf")

    assert response.status_code == 404
    assert "invoice" in response.json()["detail"].lower()


async def test_downloads_a_real_pdf_for_a_paid_order(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    await _login_admin(client, make_admin_user)
    order_id = await _checkout_simulated_paid_order(
        client, make_event, make_show, make_ticket_type, make_event_config
    )

    response = await client.get(f"/api/v1/orders/{order_id}/invoice.pdf")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert f"invoice-{order_id}.pdf" in response.headers["content-disposition"]
    assert response.content.startswith(b"%PDF")


async def test_repeated_downloads_are_each_valid_and_materially_the_same_size(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """The PDF is re-rendered fresh on every call (never persisted as a
    blob — see ``app.models.invoice.Invoice``'s module docstring), so this
    checks that repeated downloads don't drift: both must independently
    succeed as well-formed PDFs of materially the same size.

    Deliberately NOT asserting byte-for-byte equality (this test used to,
    and flaked intermittently in CI): confirmed via direct local
    reproduction that weasyprint's own font-subsetting/compression
    pipeline is not guaranteed byte-deterministic across two separate
    ``write_pdf()`` calls, even for byte-identical input HTML — two calls
    in the very same process produced outputs differing by a couple of
    bytes deep inside a ``FlateDecode``-compressed font stream, unrelated
    to anything this app's own code controls. That's a property of
    weasyprint's internals, not of this app's data determinism, so
    asserting exact byte equality here was asserting something weasyprint
    never actually promises. The thing this test actually cares about —
    that the SAME invoice data goes into both renders — is covered at the
    deterministic layer instead, by ``tests/unit/test_invoice_pdf.py``'s
    direct assertions on ``_invoice_html`` (the HTML-building step, which
    has no such non-determinism; weasyprint's PDF encoding is the only
    non-deterministic part). The size tolerance below is generous enough
    to absorb that internal jitter while still catching a genuine content
    regression (e.g. a missing line item would change the PDF's size by
    far more than a couple of bytes).
    """
    await _login_admin(client, make_admin_user)
    order_id = await _checkout_simulated_paid_order(
        client, make_event, make_show, make_ticket_type, make_event_config
    )

    first = await client.get(f"/api/v1/orders/{order_id}/invoice.pdf")
    second = await client.get(f"/api/v1/orders/{order_id}/invoice.pdf")

    assert first.status_code == second.status_code == 200
    assert first.content.startswith(b"%PDF")
    assert second.content.startswith(b"%PDF")
    assert abs(len(first.content) - len(second.content)) < 200

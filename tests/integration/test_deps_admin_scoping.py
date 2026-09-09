"""The most important test in this suite: proves ``require_admin`` (see
``app/api/deps.py``) structurally excludes every non-admin principal from
every route currently gated by it — this is the concrete enforcement of
PROJECT_BRIEF.md's "agent keys must never reach admin-only routes"
guarantee, and it must also hold for a `scanner`-role human admin, not just
agent keys.

Covers every route currently declared with ``Depends(require_admin)``:
  - POST   /api/v1/admin/agent-accounts
  - GET    /api/v1/admin/agent-accounts
  - POST   /api/v1/admin/agent-accounts/{agent_id}/revoke
  - GET    /api/v1/admin/audit-log
  - POST   /api/v1/orders/{order_id}/resend-confirmation-email (Milestone 4:
    an Order is financial/buyer-PII data, not "content-type data" — see
    ``app.api.routes.orders`` module docstring — so this is admin-only, not
    ``require_admin_or_agent``)
  - GET    /api/v1/events/{event_id}/orders (Milestone 4: same reasoning —
    listing Orders is financial/buyer-PII data, admin-only)
  - GET    /api/v1/orders/{order_id}/invoice.pdf (Milestone 5: same
    reasoning again — an Invoice is financial data, admin-only, not agent
    content-management scope — see ``app.api.routes.orders`` module
    docstring)
  - POST   /api/v1/orders/{order_id}/mark-paid (Milestone 6: manual
    mark-as-paid mutates an Order's payment status, the most sensitive
    financial action in this module — admin-only, not agent scope)
  - GET    /api/v1/events/{event_id}/stats (Milestone 8: sales/revenue
    dashboard figures are financial data — admin-only, matching every
    other financial route above — see ``app.api.routes.stats`` module
    docstring)
  - GET    /api/v1/events/{event_id}/orders/export.csv (Milestone 8: same
    reasoning — per-order financial detail for accounting export,
    admin-only)
  - POST   /api/v1/orders/{order_id}/erase-pii (Milestone 9: GDPR buyer-PII
    erasure mutates an Order's buyer fields — admin-only, same financial/
    PII reasoning as mark-paid above — see ``app.api.routes.orders``
    module docstring)
  - GET    /api/v1/orders/{order_id}/tickets.pdf (Milestone 9: standalone
    ticket-PDF re-download — same buyer-PII/financial reasoning as
    ``invoice.pdf`` above)
  - GET    /api/v1/shows/{show_id}/tickets-batch.pdf (Milestone 9: batch
    ticket-print action across every paid Order of a Show — same
    financial/PII reasoning, addressed by Show id rather than Order id)
  - POST   /api/v1/admin/admin-users (backoffice/core-management-ui:
    AdminUser account management is itself a new admin-only surface, same
    "never agent/scanner scope" reasoning as agent-account management above)
  - GET    /api/v1/admin/admin-users
  - POST   /api/v1/admin/admin-users/{admin_user_id}/deactivate
  - POST   /api/v1/admin/admin-users/{admin_user_id}/reactivate
  - POST   /api/v1/admin/admin-users/{admin_user_id}/reset-password

If a future route adds ``Depends(require_admin)``, add it to
``ADMIN_GATED_ROUTES`` below so this test keeps covering the full set.
"""

from collections.abc import Awaitable, Callable

import pytest
from httpx import AsyncClient

from app.models.enums import AdminRole
from tests.integration.conftest import SeededAdmin, SeededAgent

_PLACEHOLDER_AGENT_ID = "11111111-1111-1111-1111-111111111111"
_PLACEHOLDER_ORDER_ID = "22222222-2222-2222-2222-222222222222"
_PLACEHOLDER_EVENT_ID = "33333333-3333-3333-3333-333333333333"
_PLACEHOLDER_SHOW_ID = "44444444-4444-4444-4444-444444444444"

# (label, method, path, json_body)
ADMIN_GATED_ROUTES: list[tuple[str, str, str, dict[str, str] | None]] = [
    ("create_agent_account", "POST", "/api/v1/admin/agent-accounts", {"name": "should-not-be-created"}),
    ("list_agent_accounts", "GET", "/api/v1/admin/agent-accounts", None),
    (
        "revoke_agent_account",
        "POST",
        f"/api/v1/admin/agent-accounts/{_PLACEHOLDER_AGENT_ID}/revoke",
        None,
    ),
    ("list_audit_log", "GET", "/api/v1/admin/audit-log", None),
    (
        "resend_confirmation_email",
        "POST",
        f"/api/v1/orders/{_PLACEHOLDER_ORDER_ID}/resend-confirmation-email",
        None,
    ),
    (
        "list_event_orders",
        "GET",
        f"/api/v1/events/{_PLACEHOLDER_EVENT_ID}/orders",
        None,
    ),
    (
        "download_invoice_pdf",
        "GET",
        f"/api/v1/orders/{_PLACEHOLDER_ORDER_ID}/invoice.pdf",
        None,
    ),
    (
        "mark_order_paid",
        "POST",
        f"/api/v1/orders/{_PLACEHOLDER_ORDER_ID}/mark-paid",
        {"method_label": "test"},
    ),
    (
        "get_event_stats",
        "GET",
        f"/api/v1/events/{_PLACEHOLDER_EVENT_ID}/stats",
        None,
    ),
    (
        "export_orders_csv",
        "GET",
        f"/api/v1/events/{_PLACEHOLDER_EVENT_ID}/orders/export.csv",
        None,
    ),
    (
        "erase_order_pii",
        "POST",
        f"/api/v1/orders/{_PLACEHOLDER_ORDER_ID}/erase-pii",
        {},
    ),
    (
        "download_tickets_pdf",
        "GET",
        f"/api/v1/orders/{_PLACEHOLDER_ORDER_ID}/tickets.pdf",
        None,
    ),
    (
        "download_show_tickets_batch_pdf",
        "GET",
        f"/api/v1/shows/{_PLACEHOLDER_SHOW_ID}/tickets-batch.pdf",
        None,
    ),
    (
        "create_admin_user",
        "POST",
        "/api/v1/admin/admin-users",
        {"email": "should-not-be-created@example.test", "password": "irrelevant-password"},
    ),
    ("list_admin_users", "GET", "/api/v1/admin/admin-users", None),
    (
        "deactivate_admin_user",
        "POST",
        f"/api/v1/admin/admin-users/{_PLACEHOLDER_AGENT_ID}/deactivate",
        None,
    ),
    (
        "reactivate_admin_user",
        "POST",
        f"/api/v1/admin/admin-users/{_PLACEHOLDER_AGENT_ID}/reactivate",
        None,
    ),
    (
        "reset_admin_user_password",
        "POST",
        f"/api/v1/admin/admin-users/{_PLACEHOLDER_AGENT_ID}/reset-password",
        {"new_password": "irrelevant-password"},
    ),
]

_IDS = [route[0] for route in ADMIN_GATED_ROUTES]


async def _request(client: AsyncClient, method: str, path: str, json_body: dict[str, str] | None) -> int:
    response = await client.request(method, path, json=json_body)
    return response.status_code


@pytest.mark.parametrize(("label", "method", "path", "json_body"), ADMIN_GATED_ROUTES, ids=_IDS)
async def test_agent_key_principal_cannot_reach_admin_gated_routes(
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

    # An agent principal *is* authenticated (get_current_principal succeeds),
    # it just isn't an admin — so require_admin must reject it with 403, not
    # a 401 that would (incorrectly) suggest the key itself was invalid.
    assert status_code == 403


@pytest.mark.parametrize(("label", "method", "path", "json_body"), ADMIN_GATED_ROUTES, ids=_IDS)
async def test_scanner_role_admin_cannot_reach_admin_gated_routes(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    label: str,
    method: str,
    path: str,
    json_body: dict[str, str] | None,
) -> None:
    seeded = await make_admin_user(role=AdminRole.SCANNER)
    login = await client.post(
        "/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password}
    )
    assert login.status_code == 200

    status_code = await _request(client, method, path, json_body)

    assert status_code == 403


@pytest.mark.parametrize(("label", "method", "path", "json_body"), ADMIN_GATED_ROUTES, ids=_IDS)
async def test_unauthenticated_caller_cannot_reach_admin_gated_routes(
    client: AsyncClient, label: str, method: str, path: str, json_body: dict[str, str] | None
) -> None:
    status_code = await _request(client, method, path, json_body)

    # No credentials at all -> get_current_principal itself rejects with
    # 401, before require_admin's role check ever runs.
    assert status_code == 401


@pytest.mark.parametrize(("label", "method", "path", "json_body"), ADMIN_GATED_ROUTES, ids=_IDS)
async def test_real_admin_principal_can_reach_admin_gated_routes(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    label: str,
    method: str,
    path: str,
    json_body: dict[str, str] | None,
) -> None:
    """Positive control: proves the 403s above are actually about role
    scoping, not e.g. a routing typo that would 403/404 for everyone."""
    seeded = await make_admin_user(role=AdminRole.ADMIN)
    login = await client.post(
        "/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password}
    )
    assert login.status_code == 200

    status_code = await _request(client, method, path, json_body)

    # A real admin must never be blocked by require_admin itself. The
    # revoke route 404s for the placeholder agent id (no such account) —
    # that's still proof the request passed require_admin and reached the
    # route's own lookup logic.
    assert status_code not in (401, 403)

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

If a future route adds ``Depends(require_admin)``, add it to
``ADMIN_GATED_ROUTES`` below so this test keeps covering the full set.
"""

from collections.abc import Awaitable, Callable

import pytest
from httpx import AsyncClient

from app.models.enums import AdminRole
from tests.integration.conftest import SeededAdmin, SeededAgent

_PLACEHOLDER_AGENT_ID = "11111111-1111-1111-1111-111111111111"

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

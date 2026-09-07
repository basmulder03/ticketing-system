"""Integration tests for ``/api/v1/admin/agent-accounts`` (create/list/revoke),
authenticated as a real admin, against a real DB.

Non-admin access to these routes (agent keys, scanner-role admins,
unauthenticated) is covered in ``test_deps_admin_scoping.py`` — this file
focuses on what the routes actually do for an authorized admin.
"""

from collections.abc import Awaitable, Callable

from httpx import AsyncClient

from tests.integration.conftest import SeededAdmin, SeededAgent

# Used whenever a test needs to check the agent-key auth path *after* the
# main `client` fixture has logged in as an admin: get_current_principal
# tries the admin session cookie before the agent-key header (see
# app/api/deps.py), so reusing the same client would silently authenticate
# as the admin regardless of the (also-sent) agent header. A second,
# never-logged-in client avoids that ambiguity.


async def _login(client: AsyncClient, seeded: SeededAdmin) -> None:
    response = await client.post(
        "/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password}
    )
    assert response.status_code == 200


async def test_create_agent_account_returns_the_raw_key_exactly_once(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())

    response = await client.post("/api/v1/admin/agent-accounts", json={"name": "content-bot"})

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "content-bot"
    assert body["api_key"].startswith("bcag_")
    assert body["key_prefix"] == body["api_key"][:12]


async def test_create_agent_account_duplicate_name_returns_409(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    first = await client.post("/api/v1/admin/agent-accounts", json={"name": "content-bot"})
    assert first.status_code == 201

    second = await client.post("/api/v1/admin/agent-accounts", json={"name": "content-bot"})
    assert second.status_code == 409


async def test_list_agent_accounts_never_includes_the_raw_key(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    await client.post("/api/v1/admin/agent-accounts", json={"name": "content-bot"})

    response = await client.get("/api/v1/admin/agent-accounts")

    assert response.status_code == 200
    accounts = response.json()
    assert len(accounts) == 1
    assert accounts[0]["name"] == "content-bot"
    assert "api_key" not in accounts[0]
    assert accounts[0]["is_active"] is True


async def test_revoked_agent_key_is_rejected_on_its_next_use(
    client: AsyncClient,
    client_factory: Callable[[str | None], AsyncClient],
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_agent_account: Callable[..., Awaitable[SeededAgent]],
) -> None:
    await _login(client, await make_admin_user())
    seeded = await make_agent_account()
    headers = {"X-Agent-Api-Key": seeded.raw_key}

    async with client_factory(None) as agent_client:
        still_valid = await agent_client.get("/api/v1/auth/me", headers=headers)
        assert still_valid.status_code == 200
        assert still_valid.json()["actor_type"] == "ai_agent"

        revoke_response = await client.post(f"/api/v1/admin/agent-accounts/{seeded.account.id}/revoke")
        assert revoke_response.status_code == 200
        assert revoke_response.json()["is_active"] is False

        rejected = await agent_client.get("/api/v1/auth/me", headers=headers)
        assert rejected.status_code == 401


async def test_double_revoke_is_a_no_op_not_an_error(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_agent_account: Callable[..., Awaitable[SeededAgent]],
) -> None:
    await _login(client, await make_admin_user())
    seeded = await make_agent_account()

    first = await client.post(f"/api/v1/admin/agent-accounts/{seeded.account.id}/revoke")
    assert first.status_code == 200
    first_revoked_at = first.json()

    second = await client.post(f"/api/v1/admin/agent-accounts/{seeded.account.id}/revoke")
    assert second.status_code == 200
    assert second.json()["is_active"] is False
    # Revoking an already-revoked account is a no-op per agent_accounts.py:
    # no state change on the second call.
    assert second.json() == first_revoked_at


async def test_revoke_unknown_agent_id_returns_404(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())

    response = await client.post(
        "/api/v1/admin/agent-accounts/00000000-0000-0000-0000-000000000000/revoke"
    )
    assert response.status_code == 404


async def test_revoking_one_agent_does_not_affect_another(
    client: AsyncClient,
    client_factory: Callable[[str | None], AsyncClient],
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_agent_account: Callable[..., Awaitable[SeededAgent]],
) -> None:
    await _login(client, await make_admin_user())
    agent_a = await make_agent_account(name="agent-a")
    agent_b = await make_agent_account(name="agent-b")

    revoke_response = await client.post(f"/api/v1/admin/agent-accounts/{agent_a.account.id}/revoke")
    assert revoke_response.status_code == 200

    # A never-logged-in-as-admin client, so this genuinely exercises the
    # agent-key path rather than riding `client`'s still-valid admin cookie.
    async with client_factory(None) as agent_client:
        still_valid = await agent_client.get(
            "/api/v1/auth/me", headers={"X-Agent-Api-Key": agent_b.raw_key}
        )
        assert still_valid.status_code == 200
        assert still_valid.json()["actor_type"] == "ai_agent"

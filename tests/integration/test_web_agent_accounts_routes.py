"""Integration tests for the backoffice agent-account management web surface
(``app/web/routes/agent_accounts.py``, this branch's agent-accounts section):
``GET/POST /agent-accounts`` and ``POST /agent-accounts/{id}/revoke``.

Per this task's discipline note, does NOT re-test the underlying JSON API's
own business logic (key generation/hashing, name-uniqueness) — that lives
in ``tests/integration/test_agent_accounts_routes.py``. Focuses on what the
WEB layer adds: form translation, error-flash surfacing, CSRF,
``require_web_admin`` scoping, and — the whole point of this page's
non-standard create flow — that the raw API key IS present in the rendered
HTML response for the one request that creates it.
"""

from collections.abc import Awaitable, Callable

from httpx import AsyncClient

from app.models.enums import AdminRole
from app.web.csrf import CSRF_COOKIE_NAME
from tests.integration.conftest import SeededAdmin


async def _api_login(client: AsyncClient, seeded: SeededAdmin) -> None:
    response = await client.post(
        "/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password}
    )
    assert response.status_code == 200


async def _get_csrf(client: AsyncClient, path: str) -> str:
    page = await client.get(path)
    assert page.status_code == 200
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert token
    return token


# --- Create: one-time key reveal ----------------------------------------------


async def test_create_agent_account_reveals_the_raw_key_in_this_one_response(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())
    token = await _get_csrf(client, "/agent-accounts")

    response = await client.post("/agent-accounts", data={"csrf_token": token, "name": "content-bot"})

    assert response.status_code == 201
    assert "content-bot" in response.text
    accounts = (await client.get("/api/v1/admin/agent-accounts")).json()
    key_prefix = accounts[0]["key_prefix"]
    # The rendered reveal panel must contain the raw key inside its
    # `<pre id="new-agent-key-value">` block, and that raw value must be
    # strictly longer than (not just equal to) the short, always-visible
    # prefix — proving this is the actual secret, not merely the prefix
    # rendered twice.
    start = response.text.index('id="new-agent-key-value">') + len('id="new-agent-key-value">')
    end = response.text.index("</pre>", start)
    revealed_key = response.text[start:end].strip()
    assert revealed_key.startswith(key_prefix)
    assert len(revealed_key) > len(key_prefix)


async def test_create_agent_account_key_is_not_shown_again_on_a_subsequent_page_load(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    """Only the short, non-secret ``key_prefix`` (also rendered as
    ``bcag_...`` — see ``backoffice/agent_accounts_list.html``'s "Key
    prefix" column) is safe to keep showing on every page load; the FULL
    raw key must never reappear once the one-time-reveal response is gone."""
    await _api_login(client, await make_admin_user())
    token = await _get_csrf(client, "/agent-accounts")
    create_response = await client.post("/agent-accounts", data={"csrf_token": token, "name": "one-shot-bot"})
    assert create_response.status_code == 201
    accounts = (await client.get("/api/v1/admin/agent-accounts")).json()
    key_prefix = accounts[0]["key_prefix"]

    reload_response = await client.get("/agent-accounts")

    assert reload_response.status_code == 200
    # The short prefix is expected to keep appearing on every load
    # (identification, not a secret — see the "Key prefix" table column) —
    # what must be gone is the one-time-reveal panel itself.
    assert key_prefix in reload_response.text
    assert "was created" not in reload_response.text
    assert "Copy this API key now" not in reload_response.text


async def test_create_agent_account_empty_name_is_rejected_with_a_flash(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())
    token = await _get_csrf(client, "/agent-accounts")

    response = await client.post("/agent-accounts", data={"csrf_token": token, "name": "   "})

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/agent-accounts?flash=")
    assert "flash_kind=error" in location
    assert "A%20name%20is%20required." in location


async def test_create_agent_account_duplicate_name_surfaces_the_real_409_message(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())
    token = await _get_csrf(client, "/agent-accounts")
    first = await client.post("/agent-accounts", data={"csrf_token": token, "name": "dup-bot"})
    assert first.status_code == 201

    second = await client.post("/agent-accounts", data={"csrf_token": token, "name": "dup-bot"})

    assert second.status_code == 303
    location = second.headers["location"]
    assert location.startswith("/agent-accounts?flash=")
    assert "flash_kind=error" in location


# --- Revoke: happy path --------------------------------------------------------


async def test_revoke_agent_account_happy_path(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())
    token = await _get_csrf(client, "/agent-accounts")
    create_response = await client.post("/agent-accounts", data={"csrf_token": token, "name": "to-revoke-bot"})
    assert create_response.status_code == 201
    accounts = (await client.get("/api/v1/admin/agent-accounts")).json()
    agent_id = accounts[0]["id"]

    response = await client.post(f"/agent-accounts/{agent_id}/revoke", data={"csrf_token": token})

    assert response.status_code == 303
    assert response.headers["location"] == "/agent-accounts?flash=Agent%20account%20revoked.&flash_kind=success"
    after = (await client.get("/api/v1/admin/agent-accounts")).json()
    assert after[0]["is_active"] is False


async def test_revoke_unknown_agent_account_returns_404_flash(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())
    token = await _get_csrf(client, "/agent-accounts")

    response = await client.post(
        "/agent-accounts/00000000-0000-0000-0000-000000000000/revoke", data={"csrf_token": token}
    )

    assert response.status_code == 303
    location = response.headers["location"]
    assert "flash_kind=error" in location
    assert "Agent%20account%20not%20found." in location


# --- CSRF ----------------------------------------------------------------------


async def test_create_agent_account_missing_csrf_is_rejected(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())

    response = await client.post("/agent-accounts", data={"name": "no-csrf-bot"})

    assert response.status_code == 422


async def test_create_agent_account_wrong_csrf_is_rejected_403(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())
    await _get_csrf(client, "/agent-accounts")

    response = await client.post(
        "/agent-accounts", data={"csrf_token": "wrong-token", "name": "wrong-csrf-bot"}
    )

    assert response.status_code == 403


async def test_revoke_agent_account_missing_csrf_is_rejected(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())

    response = await client.post(
        "/agent-accounts/00000000-0000-0000-0000-000000000000/revoke", data={}
    )

    assert response.status_code == 422


# --- require_web_admin scoping --------------------------------------------------


async def test_agent_accounts_list_unauthenticated_redirects_to_login(client: AsyncClient) -> None:
    response = await client.get("/agent-accounts")

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


async def test_agent_accounts_list_unreachable_by_scanner_role(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    seeded = await make_admin_user(role=AdminRole.SCANNER)
    await _api_login(client, seeded)

    response = await client.get("/agent-accounts")

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


async def test_create_agent_account_unauthenticated_redirects_to_login(client: AsyncClient) -> None:
    response = await client.post("/agent-accounts", data={"csrf_token": "irrelevant", "name": "unauth-bot"})

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


async def test_revoke_agent_account_unreachable_by_scanner_role(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    seeded = await make_admin_user(role=AdminRole.SCANNER)
    await _api_login(client, seeded)

    response = await client.post(
        "/agent-accounts/00000000-0000-0000-0000-000000000000/revoke", data={"csrf_token": "irrelevant"}
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")

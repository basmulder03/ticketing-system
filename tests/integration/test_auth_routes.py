"""Integration tests for ``/api/v1/auth/{login,logout,me}`` against a real DB.

See ``tests/integration/conftest.py`` for the ``client``/``make_admin_user``
fixtures.
"""

from collections.abc import Awaitable, Callable

from httpx import AsyncClient

from app.core.security import ADMIN_SESSION_COOKIE_NAME
from app.models.enums import AdminRole
from tests.integration.conftest import SeededAdmin


async def test_login_success_sets_session_cookie_with_secure_flags(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    seeded = await make_admin_user(password="s3cret-pass")

    response = await client.post(
        "/api/v1/auth/login", json={"email": seeded.user.email, "password": "s3cret-pass"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["actor_type"] == "human"
    assert body["role"] == "admin"

    set_cookie = response.headers.get("set-cookie", "")
    assert ADMIN_SESSION_COOKIE_NAME in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie or "samesite=lax" in set_cookie.lower()
    # app_env=development in the test environment, so `secure` is correctly
    # NOT set (the dev stack isn't served over HTTPS) — see auth.py's
    # `secure=settings.app_env != "development"`.
    assert "Secure" not in set_cookie


async def test_login_wrong_password_returns_generic_401(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    seeded = await make_admin_user(password="s3cret-pass")

    response = await client.post(
        "/api/v1/auth/login", json={"email": seeded.user.email, "password": "wrong-password"}
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid email or password."


async def test_login_nonexistent_email_returns_the_same_generic_401(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.test", "password": "anything"}
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid email or password."


async def test_login_inactive_admin_returns_the_same_generic_401(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    seeded = await make_admin_user(password="s3cret-pass", is_active=False)

    response = await client.post(
        "/api/v1/auth/login", json={"email": seeded.user.email, "password": "s3cret-pass"}
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid email or password."


async def test_me_returns_401_when_not_authenticated(client: AsyncClient) -> None:
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 401


async def test_me_returns_the_authenticated_principal(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    seeded = await make_admin_user(password="s3cret-pass", role=AdminRole.SCANNER)
    login_response = await client.post(
        "/api/v1/auth/login", json={"email": seeded.user.email, "password": "s3cret-pass"}
    )
    assert login_response.status_code == 200

    response = await client.get("/api/v1/auth/me")

    assert response.status_code == 200
    body = response.json()
    assert body["actor_type"] == "human"
    assert body["id"] == str(seeded.user.id)
    assert body["name"] == seeded.user.email
    assert body["role"] == "scanner"


async def test_logout_clears_the_session_cookie(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    seeded = await make_admin_user(password="s3cret-pass")
    await client.post("/api/v1/auth/login", json={"email": seeded.user.email, "password": "s3cret-pass"})

    logout_response = await client.post("/api/v1/auth/logout")
    assert logout_response.status_code == 200

    me_response = await client.get("/api/v1/auth/me")
    assert me_response.status_code == 401


async def test_logout_is_idempotent_when_not_logged_in(client: AsyncClient) -> None:
    first = await client.post("/api/v1/auth/logout")
    second = await client.post("/api/v1/auth/logout")
    assert first.status_code == 200
    assert second.status_code == 200

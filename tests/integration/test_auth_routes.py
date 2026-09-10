"""Integration tests for ``/api/v1/auth/{login,logout,me}`` against a real DB.

See ``tests/integration/conftest.py`` for the ``client``/``make_admin_user``
fixtures.
"""

import uuid
from collections.abc import Awaitable, Callable

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import ADMIN_SESSION_COOKIE_NAME
from app.models.audit_log import AuditLogEntry
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


# --- Initial admin setup -----------------------------------------------------


async def test_setup_required_is_true_with_no_admins(client: AsyncClient) -> None:
    response = await client.get("/api/v1/auth/setup-required")
    assert response.status_code == 200
    assert response.json() == {"setup_required": True}


async def test_setup_required_is_false_once_an_admin_exists(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await make_admin_user()

    response = await client.get("/api/v1/auth/setup-required")

    assert response.status_code == 200
    assert response.json() == {"setup_required": False}


async def test_setup_creates_the_first_admin_and_logs_them_in(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # db_session isn't otherwise used, but its cleanup fixture deletes every
    # AdminUser row after this test — without it, the admin this test
    # creates via the bare JSON route (not `make_admin_user`, which already
    # depends on `db_session` itself) would leak into later tests and make
    # `setup_required` wrongly return False for them.
    response = await client.post(
        "/api/v1/auth/setup", json={"email": "First.Admin@Example.Test", "password": "s3cret-pass"}
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["actor_type"] == "human"
    assert body["role"] == "admin"

    set_cookie = response.headers.get("set-cookie", "")
    assert ADMIN_SESSION_COOKIE_NAME in set_cookie
    assert "HttpOnly" in set_cookie

    me_response = await client.get("/api/v1/auth/me")
    assert me_response.status_code == 200
    assert me_response.json()["role"] == "admin"

    setup_required_response = await client.get("/api/v1/auth/setup-required")
    assert setup_required_response.json() == {"setup_required": False}


async def test_setup_lowercases_the_email_matching_login(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # db_session: see the identical comment on
    # test_setup_creates_the_first_admin_and_logs_them_in above.
    await client.post("/api/v1/auth/setup", json={"email": "Mixed.Case@Example.Test", "password": "s3cret-pass"})

    login_response = await client.post(
        "/api/v1/auth/login", json={"email": "mixed.case@example.test", "password": "s3cret-pass"}
    )

    assert login_response.status_code == 200


async def test_setup_returns_409_once_an_admin_already_exists(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await make_admin_user()

    response = await client.post(
        "/api/v1/auth/setup", json={"email": "second-admin@example.test", "password": "s3cret-pass"}
    )

    assert response.status_code == 409
    assert ADMIN_SESSION_COOKIE_NAME not in response.headers.get("set-cookie", "")


async def test_setup_rejects_a_too_short_password(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/setup", json={"email": "short-pw@example.test", "password": "short"}
    )

    assert response.status_code == 422

    setup_required_response = await client.get("/api/v1/auth/setup-required")
    assert setup_required_response.json() == {"setup_required": True}


async def test_setup_writes_an_initial_setup_audit_entry(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    response = await client.post(
        "/api/v1/auth/setup", json={"email": "audited-admin@example.test", "password": "s3cret-pass"}
    )
    admin_id = response.json()["id"]

    result = await db_session.execute(
        select(AuditLogEntry)
        .where(AuditLogEntry.action == "admin_user.initial_setup")
        .where(AuditLogEntry.target_id == admin_id)
    )
    entries = result.scalars().all()
    assert len(entries) == 1
    assert entries[0].actor_id == uuid.UUID(admin_id)
    assert entries[0].detail is not None
    assert entries[0].detail["email"] == "audited-admin@example.test"

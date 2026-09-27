"""Integration tests for the initial-admin-account setup web page
(``GET``/``POST /setup``, ``app.web.routes.auth.setup_page``/
``setup_submit``) and the ``/login`` -> ``/setup`` redirect that guides a
fresh deployment's first visitor there — post-launch fix, per the user's
NOTES: "The initial admin account is currently being created by setting
the environment variables. I don't really like that flow."

Isolation note: unlike most of this suite, several tests here genuinely
depend on a GLOBAL invariant ("zero AdminUser rows exist at all"), not just
their own data. Every test that creates an admin via the bare ``/setup``
route (not the ``make_admin_user`` fixture, which already depends on
``db_session`` and so cleans up after itself) explicitly takes
``db_session`` as a parameter too, so its cleanup fixture deletes that row
afterward — without this, it would leak into later tests and make
``setup_required`` wrongly report ``False`` for them. See
``tests/integration/test_auth_routes.py``'s equivalent JSON-API tests for
the same reasoning, and where this was actually caught.
"""

from collections.abc import Awaitable, Callable

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import ADMIN_SESSION_COOKIE_NAME
from app.web.csrf import CSRF_COOKIE_NAME
from tests.integration.conftest import SeededAdmin


async def _get_csrf_token(client: AsyncClient, path: str) -> str:
    response = await client.get(path)
    assert response.status_code == 200
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert token
    return token


# --- /login <-> /setup redirect ----------------------------------------------


async def test_login_page_redirects_to_setup_when_no_admin_exists(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    response = await client.get("/login")

    assert response.status_code == 303
    assert response.headers["location"] == "/setup"


async def test_login_page_shows_the_normal_form_once_an_admin_exists(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await make_admin_user()

    response = await client.get("/login")

    assert response.status_code == 200
    assert "Log in" in response.text


async def test_setup_page_redirects_to_login_once_an_admin_exists(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await make_admin_user()

    response = await client.get("/setup")

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_setup_page_renders_the_form_when_no_admin_exists(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    response = await client.get("/setup")

    assert response.status_code == 200
    assert "Set up your admin account" in response.text
    assert 'name="confirm_password"' in response.text


# --- Setup submit: happy path -------------------------------------------------


async def test_setup_submit_happy_path_creates_admin_and_logs_in(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _get_csrf_token(client, "/setup")

    response = await client.post(
        "/setup",
        data={
            "csrf_token": token,
            "email": "new-deployer@example.test",
            "password": "s3cret-pass",
            "confirm_password": "s3cret-pass",
        },
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/events"
    assert ADMIN_SESSION_COOKIE_NAME in response.cookies

    me_response = await client.get("/api/v1/auth/me")
    assert me_response.status_code == 200
    assert me_response.json()["role"] == "admin"


# --- Setup submit: validation/error surfacing --------------------------------


async def test_setup_submit_password_mismatch_shows_error_and_creates_nothing(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _get_csrf_token(client, "/setup")

    response = await client.post(
        "/setup",
        data={
            "csrf_token": token,
            "email": "typo-buyer@example.test",
            "password": "s3cret-pass",
            "confirm_password": "totally-different",
        },
    )

    assert response.status_code == 422
    assert "Passwords do not match" in response.text
    assert ADMIN_SESSION_COOKIE_NAME not in response.cookies

    setup_required = await client.get("/api/v1/auth/setup-required")
    assert setup_required.json() == {"setup_required": True}


async def test_setup_submit_after_setup_already_done_surfaces_the_conflict(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await make_admin_user()
    token = await _get_csrf_token(client, "/login")  # /setup itself now redirects, so get the cookie via /login

    response = await client.post(
        "/setup",
        data={
            "csrf_token": token,
            "email": "too-late@example.test",
            "password": "s3cret-pass",
            "confirm_password": "s3cret-pass",
        },
    )

    assert response.status_code == 422
    assert "already been completed" in response.text


async def test_setup_submit_missing_csrf_is_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    response = await client.post(
        "/setup",
        data={"email": "no-csrf@example.test", "password": "s3cret-pass", "confirm_password": "s3cret-pass"},
    )

    assert response.status_code == 422  # required Form(...) field entirely absent
    assert ADMIN_SESSION_COOKIE_NAME not in response.cookies


async def test_setup_submit_wrong_csrf_is_rejected_403(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _get_csrf_token(client, "/setup")  # sets a real cookie...

    response = await client.post(
        "/setup",
        data={
            "csrf_token": "not-the-real-token",  # ...but we submit a different value
            "email": "wrong-csrf@example.test",
            "password": "s3cret-pass",
            "confirm_password": "s3cret-pass",
        },
    )

    assert response.status_code == 403
    assert ADMIN_SESSION_COOKIE_NAME not in response.cookies

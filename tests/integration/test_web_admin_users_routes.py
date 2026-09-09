"""Integration tests for the backoffice AdminUser account management web
surface (``app/web/routes/admin_users.py``, this branch's AdminUser-web-UI
section): ``GET/POST /admin-users`` and its deactivate/reactivate/
reset-password actions.

Per this task's discipline note, does NOT re-test the underlying JSON API's
own business logic (self-deactivation guard, deactivation-blocks-login) —
that lives in ``tests/integration/test_admin_users_route.py``. Focuses on
what the WEB layer adds: form translation, error-flash surfacing
(including the 401 self-reset-wrong-password case), CSRF,
``require_web_admin`` scoping, and — called out explicitly as "genuinely
tricky" by this branch's task brief — the template's conditional
self-row logic (no deactivate action on your own row, ``current_password``
only on your own row's reset-password form).

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


# --- Create/list/deactivate/reactivate/reset-password happy paths ------------


async def test_create_admin_user_happy_path_redirects_with_success_flash(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())
    token = await _get_csrf(client, "/admin-users")

    response = await client.post(
        "/admin-users",
        data={"csrf_token": token, "email": "new-admin@example.test", "password": "a-long-password", "role": "admin"},
    )

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/admin-users?flash=")
    assert "flash_kind=success" in location
    assert "new-admin%40example.test" in location or "new-admin@example.test" in location

    list_page = await client.get("/admin-users")
    assert "new-admin@example.test" in list_page.text


async def test_create_admin_user_duplicate_email_surfaces_the_real_409_message(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())
    token = await _get_csrf(client, "/admin-users")
    first = await client.post(
        "/admin-users", data={"csrf_token": token, "email": "dup-web@example.test", "password": "a-long-password"}
    )
    assert first.status_code == 303 and "flash_kind=success" in first.headers["location"]

    second = await client.post(
        "/admin-users", data={"csrf_token": token, "email": "dup-web@example.test", "password": "a-long-password"}
    )

    assert second.status_code == 303
    location = second.headers["location"]
    assert "flash_kind=error" in location
    assert "already%20exists" in location


async def test_admin_users_list_renders_every_account(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())
    await make_admin_user(email="listed-one@example.test")
    await make_admin_user(email="listed-two@example.test")

    response = await client.get("/admin-users")

    assert response.status_code == 200
    assert "listed-one@example.test" in response.text
    assert "listed-two@example.test" in response.text


async def test_deactivate_web_happy_path(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())
    target = await make_admin_user(email="deactivate-web@example.test")
    token = await _get_csrf(client, "/admin-users")

    response = await client.post(f"/admin-users/{target.user.id}/deactivate", data={"csrf_token": token})

    assert response.status_code == 303
    assert response.headers["location"] == "/admin-users?flash=Admin%20user%20deactivated.&flash_kind=success"
    after = await client.get("/api/v1/admin/admin-users")
    account = next(a for a in after.json() if a["id"] == str(target.user.id))
    assert account["is_active"] is False


async def test_reactivate_web_happy_path(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())
    target = await make_admin_user(email="reactivate-web@example.test")
    token = await _get_csrf(client, "/admin-users")
    deactivate = await client.post(f"/admin-users/{target.user.id}/deactivate", data={"csrf_token": token})
    assert deactivate.status_code == 303

    response = await client.post(f"/admin-users/{target.user.id}/reactivate", data={"csrf_token": token})

    assert response.status_code == 303
    assert response.headers["location"] == "/admin-users?flash=Admin%20user%20reactivated.&flash_kind=success"


async def test_reset_password_web_happy_path_for_a_different_account(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())
    target = await make_admin_user(email="reset-web@example.test")
    token = await _get_csrf(client, "/admin-users")

    response = await client.post(
        f"/admin-users/{target.user.id}/reset-password",
        data={"csrf_token": token, "new_password": "an-admin-assisted-password"},
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin-users?flash=Password%20reset.&flash_kind=success"


async def test_reset_own_password_web_wrong_current_password_surfaces_401_as_flash(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    seeded = await make_admin_user()
    await _api_login(client, seeded)
    token = await _get_csrf(client, "/admin-users")

    response = await client.post(
        f"/admin-users/{seeded.user.id}/reset-password",
        data={"csrf_token": token, "new_password": "new-password-here", "current_password": "wrong-one"},
    )

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/admin-users?flash=")
    assert "flash_kind=error" in location


async def test_reset_own_password_web_correct_current_password_succeeds(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    seeded = await make_admin_user()
    await _api_login(client, seeded)
    token = await _get_csrf(client, "/admin-users")

    response = await client.post(
        f"/admin-users/{seeded.user.id}/reset-password",
        data={"csrf_token": token, "new_password": "new-password-here", "current_password": seeded.password},
    )

    assert response.status_code == 303
    assert "flash_kind=success" in response.headers["location"]


# --- Template conditional logic: self-row hiding/current_password ------------


async def test_own_row_has_no_deactivate_action_but_other_rows_do(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    seeded = await make_admin_user(email="self-admin@example.test")
    await _api_login(client, seeded)
    other = await make_admin_user(email="other-admin@example.test")

    response = await client.get("/admin-users")

    assert response.status_code == 200
    html = response.text
    own_deactivate_action = f'action="/admin-users/{seeded.user.id}/deactivate"'
    other_deactivate_action = f'action="/admin-users/{other.user.id}/deactivate"'
    assert own_deactivate_action not in html
    assert other_deactivate_action in html


async def test_own_reset_password_form_includes_current_password_others_do_not(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    seeded = await make_admin_user(email="self-reset@example.test")
    await _api_login(client, seeded)
    other = await make_admin_user(email="other-reset@example.test")

    response = await client.get("/admin-users")

    assert response.status_code == 200
    html = response.text

    own_form_start = html.index(f'action="/admin-users/{seeded.user.id}/reset-password"')
    own_form_end = html.index("</form>", own_form_start)
    own_form_html = html[own_form_start:own_form_end]
    assert f'id="current-password-{seeded.user.id}"' in own_form_html
    assert 'name="current_password"' in own_form_html

    other_form_start = html.index(f'action="/admin-users/{other.user.id}/reset-password"')
    other_form_end = html.index("</form>", other_form_start)
    other_form_html = html[other_form_start:other_form_end]
    assert "current_password" not in other_form_html


# --- CSRF ----------------------------------------------------------------------


async def test_create_admin_user_missing_csrf_is_rejected(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())

    response = await client.post(
        "/admin-users", data={"email": "no-csrf@example.test", "password": "a-long-password"}
    )

    assert response.status_code == 422


async def test_create_admin_user_wrong_csrf_is_rejected_403(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())
    await _get_csrf(client, "/admin-users")

    response = await client.post(
        "/admin-users",
        data={"csrf_token": "wrong-token", "email": "wrong-csrf@example.test", "password": "a-long-password"},
    )

    assert response.status_code == 403


async def test_deactivate_web_missing_csrf_is_rejected(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())
    target = await make_admin_user(email="csrf-missing-target@example.test")

    response = await client.post(f"/admin-users/{target.user.id}/deactivate", data={})

    assert response.status_code == 422


# --- require_web_admin scoping --------------------------------------------------


async def test_admin_users_list_unauthenticated_redirects_to_login(client: AsyncClient) -> None:
    response = await client.get("/admin-users")

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


async def test_admin_users_list_unreachable_by_scanner_role(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    seeded = await make_admin_user(role=AdminRole.SCANNER)
    await _api_login(client, seeded)

    response = await client.get("/admin-users")

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


async def test_create_admin_user_unauthenticated_redirects_to_login(client: AsyncClient) -> None:
    response = await client.post(
        "/admin-users", data={"csrf_token": "irrelevant", "email": "x@example.test", "password": "a-long-password"}
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


async def test_deactivate_web_unreachable_by_scanner_role(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    target = await make_admin_user(email="scanner-cant-deactivate@example.test")
    seeded = await make_admin_user(role=AdminRole.SCANNER)
    await _api_login(client, seeded)

    response = await client.post(f"/admin-users/{target.user.id}/deactivate", data={"csrf_token": "irrelevant"})

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")

"""Integration tests for ``/api/v1/admin/admin-users`` (create/list/
deactivate/reactivate/reset-password), authenticated as a real admin,
against a real DB.

Mirrors ``tests/integration/test_agent_accounts_routes.py``'s conventions.
Non-admin access to these routes (agent keys, scanner-role admins,
unauthenticated) is registered in ``test_deps_admin_scoping.py``'s shared
``ADMIN_GATED_ROUTES`` table, not repeated here.

This file's two most important tests, called out explicitly by this
branch's task brief: the **self-deactivation 409 guard**
(``test_self_deactivation_is_rejected_with_409``), and that deactivation
**actually blocks login and invalidates an existing session on its very
next request** (``test_deactivated_account_cannot_log_in`` /
``test_an_existing_session_is_rejected_on_its_next_request_after_deactivation``)
— proving ``app.api.deps._load_admin_principal``'s claimed re-check, not
just trusting its docstring. Both hold up under direct testing.

**Real bug found while writing this file, flagged for backend-builder, NOT
worked around here:** ``create_admin_user`` (``app.api.routes.admin_users``)
calls ``session.flush()`` to populate ``admin.id`` for the audit-log entry
*before* ``commit_or_conflict``'s try/except runs — but the INSERT (and any
unique-constraint violation on ``email``) is actually executed by that
early ``flush()``, not by the later ``commit()``. A duplicate email
therefore raises an unhandled ``IntegrityError`` straight through to a raw
500, never reaching ``commit_or_conflict``'s 409 translation at all. Every
other create route with a uniqueness constraint in this codebase
(``app.api.routes.events.create_event``, e.g.) avoids this by pre-checking
uniqueness with an explicit ``SELECT`` before insert, specifically because
of this exact flush-vs-commit ordering hazard — ``create_admin_user`` is
the one route that doesn't follow that convention. The two tests below
that exercise this path are marked ``xfail(strict=True)`` so this stays
visible in CI (a strict-xfail flips to a hard failure the moment the bug is
fixed, forcing the marker to be removed) rather than silently skipped.
"""

from collections.abc import Awaitable, Callable

import pytest
from httpx import AsyncClient

from app.models.enums import AdminRole
from tests.integration.conftest import SeededAdmin


async def _login(client: AsyncClient, seeded: SeededAdmin) -> None:
    response = await client.post(
        "/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password}
    )
    assert response.status_code == 200


# --- Create --------------------------------------------------------------------


async def test_create_admin_user_lowercases_email_and_never_returns_the_password(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())

    response = await client.post(
        "/api/v1/admin/admin-users",
        json={"email": "Mixed.Case@Example.Test", "password": "a-genuinely-long-password", "role": "admin"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "mixed.case@example.test"
    assert "password" not in body
    assert "hashed_password" not in body


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Real bug: create_admin_user's session.flush() (to populate admin.id "
        "for the audit entry) executes the INSERT before commit_or_conflict's "
        "try/except runs, so a duplicate email's IntegrityError escapes as an "
        "unhandled 500 instead of the intended 409. See this module's "
        "docstring. Remove this marker once app.api.routes.admin_users."
        "create_admin_user pre-checks email uniqueness the way "
        "app.api.routes.events.create_event pre-checks slug uniqueness."
    ),
)
async def test_create_admin_user_duplicate_email_returns_409(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    first = await client.post(
        "/api/v1/admin/admin-users",
        json={"email": "dup@example.test", "password": "a-genuinely-long-password"},
    )
    assert first.status_code == 201

    second = await client.post(
        "/api/v1/admin/admin-users",
        json={"email": "dup@example.test", "password": "another-long-password"},
    )
    assert second.status_code == 409


# --- List ------------------------------------------------------------------------


async def test_list_admin_users_never_leaks_the_password_hash(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    await client.post(
        "/api/v1/admin/admin-users",
        json={"email": "listed@example.test", "password": "a-genuinely-long-password"},
    )

    response = await client.get("/api/v1/admin/admin-users")

    assert response.status_code == 200
    for account in response.json():
        assert "hashed_password" not in account
        assert "password" not in account


# --- Deactivate: self-guard, other-account, idempotency, 404 -------------------


async def test_self_deactivation_is_rejected_with_409(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    seeded = await make_admin_user()
    await _login(client, seeded)

    response = await client.post(f"/api/v1/admin/admin-users/{seeded.user.id}/deactivate")

    assert response.status_code == 409
    # The calling admin must genuinely remain active.
    still_works = await client.get("/api/v1/auth/me")
    assert still_works.status_code == 200


async def test_deactivating_a_different_account_succeeds(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    target = await make_admin_user(email="target@example.test")

    response = await client.post(f"/api/v1/admin/admin-users/{target.user.id}/deactivate")

    assert response.status_code == 200
    assert response.json()["is_active"] is False


async def test_deactivating_an_already_inactive_account_is_a_safe_no_op(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    target = await make_admin_user(email="already-inactive@example.test")
    first = await client.post(f"/api/v1/admin/admin-users/{target.user.id}/deactivate")
    assert first.status_code == 200

    second = await client.post(f"/api/v1/admin/admin-users/{target.user.id}/deactivate")

    assert second.status_code == 200
    assert second.json()["is_active"] is False


async def test_deactivate_unknown_admin_user_id_returns_404(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())

    response = await client.post(
        "/api/v1/admin/admin-users/00000000-0000-0000-0000-000000000000/deactivate"
    )

    assert response.status_code == 404


# --- Deactivation actually blocks login / kills an existing session ------------


async def test_deactivated_account_cannot_log_in(
    client: AsyncClient,
    client_factory: Callable[[str | None], AsyncClient],
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
) -> None:
    await _login(client, await make_admin_user())
    target = await make_admin_user(email="cant-login@example.test")

    deactivate = await client.post(f"/api/v1/admin/admin-users/{target.user.id}/deactivate")
    assert deactivate.status_code == 200

    async with client_factory(None) as target_client:
        login_attempt = await target_client.post(
            "/api/v1/auth/login", json={"email": target.user.email, "password": target.password}
        )
        assert login_attempt.status_code == 401


async def test_an_existing_session_is_rejected_on_its_next_request_after_deactivation(
    client: AsyncClient,
    client_factory: Callable[[str | None], AsyncClient],
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
) -> None:
    """Verifies ``app.api.deps._load_admin_principal``'s ``is_active``
    re-check on every request, per that route module's own docstring claim
    — proven directly rather than trusted."""
    admin = await make_admin_user()
    await _login(client, admin)
    target = await make_admin_user(email="live-session@example.test")

    async with client_factory(None) as target_client:
        target_login = await target_client.post(
            "/api/v1/auth/login", json={"email": target.user.email, "password": target.password}
        )
        assert target_login.status_code == 200
        still_valid = await target_client.get("/api/v1/auth/me")
        assert still_valid.status_code == 200

        deactivate = await client.post(f"/api/v1/admin/admin-users/{target.user.id}/deactivate")
        assert deactivate.status_code == 200

        rejected = await target_client.get("/api/v1/auth/me")
        assert rejected.status_code == 401


# --- Reactivate: happy path / idempotency ---------------------------------------


async def test_reactivate_happy_path_restores_login_access(
    client: AsyncClient,
    client_factory: Callable[[str | None], AsyncClient],
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
) -> None:
    await _login(client, await make_admin_user())
    target = await make_admin_user(email="reactivate-me@example.test")
    deactivate = await client.post(f"/api/v1/admin/admin-users/{target.user.id}/deactivate")
    assert deactivate.status_code == 200

    reactivate = await client.post(f"/api/v1/admin/admin-users/{target.user.id}/reactivate")

    assert reactivate.status_code == 200
    assert reactivate.json()["is_active"] is True

    async with client_factory(None) as target_client:
        login_attempt = await target_client.post(
            "/api/v1/auth/login", json={"email": target.user.email, "password": target.password}
        )
        assert login_attempt.status_code == 200


async def test_reactivating_an_already_active_account_is_a_safe_no_op(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    target = await make_admin_user(email="already-active@example.test")

    response = await client.post(f"/api/v1/admin/admin-users/{target.user.id}/reactivate")

    assert response.status_code == 200
    assert response.json()["is_active"] is True


# --- Reset password: self vs other, current_password rules, audit content -----


async def test_self_reset_without_current_password_is_rejected_401(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    seeded = await make_admin_user()
    await _login(client, seeded)

    response = await client.post(
        f"/api/v1/admin/admin-users/{seeded.user.id}/reset-password",
        json={"new_password": "a-brand-new-password"},
    )

    assert response.status_code == 401


async def test_self_reset_with_wrong_current_password_is_rejected_401(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    seeded = await make_admin_user()
    await _login(client, seeded)

    response = await client.post(
        f"/api/v1/admin/admin-users/{seeded.user.id}/reset-password",
        json={"new_password": "a-brand-new-password", "current_password": "totally-wrong"},
    )

    assert response.status_code == 401


async def test_self_reset_with_correct_current_password_succeeds_and_new_password_works(
    client: AsyncClient,
    client_factory: Callable[[str | None], AsyncClient],
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
) -> None:
    seeded = await make_admin_user()
    await _login(client, seeded)

    response = await client.post(
        f"/api/v1/admin/admin-users/{seeded.user.id}/reset-password",
        json={"new_password": "a-brand-new-password", "current_password": seeded.password},
    )

    assert response.status_code == 200

    async with client_factory(None) as fresh_client:
        login_with_new_password = await fresh_client.post(
            "/api/v1/auth/login", json={"email": seeded.user.email, "password": "a-brand-new-password"}
        )
        assert login_with_new_password.status_code == 200
        login_with_old_password = await fresh_client.post(
            "/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password}
        )
        assert login_with_old_password.status_code == 401


async def test_resetting_a_different_accounts_password_requires_no_current_password(
    client: AsyncClient,
    client_factory: Callable[[str | None], AsyncClient],
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
) -> None:
    await _login(client, await make_admin_user())
    target = await make_admin_user(email="reset-target@example.test")

    response = await client.post(
        f"/api/v1/admin/admin-users/{target.user.id}/reset-password",
        json={"new_password": "an-admin-assisted-new-password"},
    )

    assert response.status_code == 200

    async with client_factory(None) as fresh_client:
        login_attempt = await fresh_client.post(
            "/api/v1/auth/login",
            json={"email": target.user.email, "password": "an-admin-assisted-new-password"},
        )
        assert login_attempt.status_code == 200


async def test_reset_password_audit_detail_never_contains_the_plaintext_password(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    target = await make_admin_user(email="audit-no-leak@example.test")

    secret_new_password = "super-secret-new-password-xyz"
    reset = await client.post(
        f"/api/v1/admin/admin-users/{target.user.id}/reset-password",
        json={"new_password": secret_new_password},
    )
    assert reset.status_code == 200

    audit = await client.get("/api/v1/admin/audit-log", params={"limit": 20})
    assert audit.status_code == 200
    entries = audit.json()
    password_reset_entries = [e for e in entries if e["action"] == "admin_user.password_reset"]
    assert password_reset_entries
    for entry in password_reset_entries:
        assert secret_new_password not in str(entry)


async def test_reset_password_unknown_admin_user_id_returns_404(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())

    response = await client.post(
        "/api/v1/admin/admin-users/00000000-0000-0000-0000-000000000000/reset-password",
        json={"new_password": "irrelevant-password"},
    )

    assert response.status_code == 404


# --- Scanner-role principal cannot deactivate itself either ---------------------


async def test_scanner_role_cannot_reach_admin_users_routes_at_all(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    """Sanity check that a scanner-role account is blocked by
    ``require_admin`` before any of this module's own logic runs — the full
    parametrized sweep lives in ``test_deps_admin_scoping.py``."""
    seeded = await make_admin_user(role=AdminRole.SCANNER)
    await _login(client, seeded)

    response = await client.get("/api/v1/admin/admin-users")

    assert response.status_code == 403

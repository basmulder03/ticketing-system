"""Integration tests for ``/api/v1/events/{event_id}/config``, authenticated
as a real admin, against a real DB.

Non-admin/non-agent access is covered in ``test_deps_content_scoping.py``.
This file covers what the routes/actions actually do: secret redaction
(response body AND the raw DB column), PUT partial-update semantics for
omitted vs. explicit-null fields, the SMTP/Mollie connection-test actions,
and "copy configuration from previous event".

Connection-test coverage notes:
- SMTP: a real send against the Mailpit sink already running in the local
  dev/CI compose stack (``docker-compose.yml``'s ``mailpit`` service) — a
  genuine network round trip, not a mock, per PROJECT_BRIEF.md's "SMTP sink
  for local dev: Mailpit" convention. CI wires up an equivalent Mailpit
  service (see ``.github/workflows/ci.yml``) so this runs there too.
- Mollie: hits the real ``https://api.mollie.com/v2/methods`` endpoint with
  a deliberately invalid key to prove a bad key produces a clean
  ``success=False`` result rather than a 500 — a real network call (no
  mock), matching the brief's Mollie-test-mode-only rule (no live key,
  no payment created, just a read-only key check against Mollie's own
  test-mode-agnostic validation endpoint). If this ever proves flaky in CI
  (real third-party network dependency), replacing it with an ``httpx``
  mock is a reasonable follow-up for a future milestone's payments work.
"""

from collections.abc import Awaitable, Callable

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from tests.integration.conftest import SeededAdmin


async def _login(client: AsyncClient, seeded: SeededAdmin) -> None:
    response = await client.post("/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password})
    assert response.status_code == 200


async def _create_event(client: AsyncClient, slug: str = "config-test-event") -> str:
    response = await client.post("/api/v1/events", json={"name": "Config Test Event", "slug": slug})
    assert response.status_code == 201
    return str(response.json()["id"])


# --- Secret redaction ---


async def test_put_response_never_echoes_raw_secrets(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    event_id = await _create_event(client)

    response = await client.put(
        f"/api/v1/events/{event_id}/config",
        json={
            "smtp_password": "super-secret-smtp-password",
            "mollie_test_api_key": "test_abc123",
            "mollie_live_api_key": "live_xyz789",
        },
    )

    assert response.status_code == 200
    body = response.json()
    raw = response.text
    assert "super-secret-smtp-password" not in raw
    assert "test_abc123" not in raw
    assert "live_xyz789" not in raw
    assert body["smtp_password_is_set"] is True
    assert body["mollie_test_api_key_is_set"] is True
    assert body["mollie_live_api_key_is_set"] is True
    assert "smtp_password" not in body
    assert "mollie_test_api_key" not in body
    assert "mollie_live_api_key" not in body


async def test_get_response_never_echoes_raw_secrets(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    event_id = await _create_event(client)
    await client.put(f"/api/v1/events/{event_id}/config", json={"smtp_password": "another-secret-value"})

    response = await client.get(f"/api/v1/events/{event_id}/config")

    assert response.status_code == 200
    assert "another-secret-value" not in response.text
    assert response.json()["smtp_password_is_set"] is True


async def test_audit_log_never_contains_raw_secret_plaintext(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], db_session: AsyncSession
) -> None:
    await _login(client, await make_admin_user())
    event_id = await _create_event(client)

    await client.put(
        f"/api/v1/events/{event_id}/config",
        json={"smtp_password": "should-never-appear-in-audit-log", "mollie_test_api_key": "test_should_be_redacted"},
    )

    result = await db_session.execute(
        text("SELECT detail FROM audit_log_entries WHERE action = 'event_config.create'")
    )
    rows = result.scalars().all()
    assert len(rows) == 1
    detail = rows[0]
    assert detail["smtp_password"] == "<redacted>"
    assert detail["mollie_test_api_key"] == "<redacted>"


async def test_underlying_db_column_is_encrypted_not_plaintext(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], db_session: AsyncSession
) -> None:
    """Bypasses the ORM's ``EncryptedString`` type decorator with a raw SQL
    read, so this proves the *stored* bytes are ciphertext — not just that
    the API layer happens to redact it on the way out."""
    await _login(client, await make_admin_user())
    event_id = await _create_event(client)
    plaintext = "raw-column-should-not-equal-this"

    put_response = await client.put(f"/api/v1/events/{event_id}/config", json={"smtp_password": plaintext})
    config_id = put_response.json()["id"]

    result = await db_session.execute(
        text("SELECT smtp_password FROM event_configs WHERE id = :id"), {"id": config_id}
    )
    raw_stored_value = result.scalar_one()

    assert raw_stored_value != plaintext
    assert plaintext not in raw_stored_value
    # Fernet tokens are versioned base64url tokens starting with "gAAAAA"
    # (version byte 0x80 + timestamp, base64-encoded) — a cheap structural
    # signal this is really ciphertext, not e.g. an unencrypted passthrough.
    assert raw_stored_value.startswith("gAAAAA")


# --- PUT partial-update semantics ---


async def test_omitted_field_on_put_leaves_existing_value_unchanged(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    event_id = await _create_event(client)
    await client.put(f"/api/v1/events/{event_id}/config", json={"smtp_host": "original-host.example"})

    response = await client.put(f"/api/v1/events/{event_id}/config", json={"smtp_port": 587})

    assert response.status_code == 200
    body = response.json()
    assert body["smtp_host"] == "original-host.example"
    assert body["smtp_port"] == 587


async def test_explicit_empty_string_clears_a_secret(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    event_id = await _create_event(client)
    await client.put(f"/api/v1/events/{event_id}/config", json={"smtp_password": "to-be-cleared"})
    assert (await client.get(f"/api/v1/events/{event_id}/config")).json()["smtp_password_is_set"] is True

    response = await client.put(f"/api/v1/events/{event_id}/config", json={"smtp_password": ""})

    assert response.status_code == 200
    assert response.json()["smtp_password_is_set"] is False


async def test_explicit_null_also_clears_a_secret(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    """Documents actual behavior (not assumed): ``EventConfigUpdateRequest``
    uses Pydantic's ``exclude_unset`` (see ``apply_partial_update``), which
    only distinguishes "key absent from the JSON body" from "key present"
    — an explicit ``null`` is a present key with value ``None``, so it is
    applied exactly like the documented empty-string case, clearing the
    secret. Only a fully *omitted* key leaves the existing value alone."""
    await _login(client, await make_admin_user())
    event_id = await _create_event(client)
    await client.put(f"/api/v1/events/{event_id}/config", json={"smtp_password": "to-be-cleared"})

    response = await client.put(f"/api/v1/events/{event_id}/config", json={"smtp_password": None})

    assert response.status_code == 200
    assert response.json()["smtp_password_is_set"] is False


async def test_explicit_null_smtp_encryption_resets_to_none_not_rejected(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    event_id = await _create_event(client)
    await client.put(f"/api/v1/events/{event_id}/config", json={"smtp_encryption": "ssl"})

    response = await client.put(f"/api/v1/events/{event_id}/config", json={"smtp_encryption": None})

    assert response.status_code == 200
    assert response.json()["smtp_encryption"] == "none"


async def test_explicit_null_enabled_payment_methods_resets_to_empty_list(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    event_id = await _create_event(client)
    await client.put(f"/api/v1/events/{event_id}/config", json={"enabled_payment_methods": ["mollie", "door"]})

    response = await client.put(f"/api/v1/events/{event_id}/config", json={"enabled_payment_methods": None})

    assert response.status_code == 200
    assert response.json()["enabled_payment_methods"] == []


async def test_put_creates_config_on_first_call_and_updates_on_second(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    event_id = await _create_event(client)
    assert (await client.get(f"/api/v1/events/{event_id}/config")).status_code == 404

    first = await client.put(f"/api/v1/events/{event_id}/config", json={"smtp_host": "host-a"})
    assert first.status_code == 200
    config_id = first.json()["id"]

    second = await client.put(f"/api/v1/events/{event_id}/config", json={"smtp_host": "host-b"})
    assert second.status_code == 200
    # Same underlying row (upsert), not a duplicate.
    assert second.json()["id"] == config_id
    assert second.json()["smtp_host"] == "host-b"


# --- Connection-test actions ---


async def test_smtp_connection_test_succeeds_against_mailpit(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    event_id = await _create_event(client)
    settings = get_settings()
    await client.put(
        f"/api/v1/events/{event_id}/config",
        json={
            "smtp_host": settings.seed_smtp_host,
            "smtp_port": settings.seed_smtp_port,
            "sender_email": "sender@example.test",
        },
    )

    response = await client.post(
        f"/api/v1/events/{event_id}/config/test-email", json={"recipient": "recipient@example.test"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert "recipient@example.test" in body["message"]


async def test_smtp_connection_test_fails_cleanly_when_unconfigured(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    event_id = await _create_event(client)
    await client.put(f"/api/v1/events/{event_id}/config", json={"smtp_host": None})

    response = await client.post(
        f"/api/v1/events/{event_id}/config/test-email", json={"recipient": "recipient@example.test"}
    )

    assert response.status_code == 200
    assert response.json()["success"] is False


async def test_smtp_connection_test_fails_cleanly_against_an_unreachable_host(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    """A real network failure (nothing listening) must produce a handled
    ``success=False`` result, never an unhandled 500."""
    await _login(client, await make_admin_user())
    event_id = await _create_event(client)
    await client.put(
        f"/api/v1/events/{event_id}/config",
        json={"smtp_host": "127.0.0.1", "smtp_port": 1, "sender_email": "sender@example.test"},
    )

    response = await client.post(
        f"/api/v1/events/{event_id}/config/test-email", json={"recipient": "recipient@example.test"}
    )

    assert response.status_code == 200
    assert response.json()["success"] is False


async def test_mollie_connection_test_fails_cleanly_with_an_invalid_key(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    """Real call to Mollie's API (test mode is irrelevant here — this key is
    simply invalid/malformed, never a real live/test credential) — asserts
    the failure is a clean handled result, not a 500."""
    await _login(client, await make_admin_user())
    event_id = await _create_event(client)
    await client.put(
        f"/api/v1/events/{event_id}/config",
        json={"mollie_test_api_key": "test_this_is_definitely_not_a_real_mollie_key_000"},
    )

    response = await client.post(f"/api/v1/events/{event_id}/config/test-mollie", json={"environment": "test"})

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["message"]


async def test_mollie_connection_test_fails_cleanly_when_no_key_configured(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    event_id = await _create_event(client)
    await client.put(f"/api/v1/events/{event_id}/config", json={"smtp_host": "mailpit"})

    response = await client.post(f"/api/v1/events/{event_id}/config/test-mollie", json={"environment": "live"})

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert "No Mollie API key" in body["message"]


async def test_mollie_connection_test_selects_test_vs_live_key_independently(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    """Setting only the test key must not make the live-environment check
    report success — the two are verified independently."""
    await _login(client, await make_admin_user())
    event_id = await _create_event(client)
    await client.put(f"/api/v1/events/{event_id}/config", json={"mollie_test_api_key": "test_only_this_one_is_set"})

    live_check = await client.post(f"/api/v1/events/{event_id}/config/test-mollie", json={"environment": "live"})

    assert live_check.status_code == 200
    assert live_check.json()["success"] is False
    assert "No Mollie API key" in live_check.json()["message"]


async def test_connection_test_actions_are_not_written_to_the_audit_log(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], db_session: AsyncSession
) -> None:
    await _login(client, await make_admin_user())
    event_id = await _create_event(client)
    await client.put(
        f"/api/v1/events/{event_id}/config",
        json={"smtp_host": "mailpit", "smtp_port": 1025, "sender_email": "sender@example.test"},
    )

    result_before = await db_session.execute(text("SELECT count(*) FROM audit_log_entries"))
    count_before = result_before.scalar_one()

    await client.post(f"/api/v1/events/{event_id}/config/test-email", json={"recipient": "x@example.test"})
    await client.post(f"/api/v1/events/{event_id}/config/test-mollie", json={"environment": "test"})

    result_after = await db_session.execute(text("SELECT count(*) FROM audit_log_entries"))
    count_after = result_after.scalar_one()

    assert count_after == count_before


# --- "Copy configuration from previous event" ---


async def test_copy_config_copies_fields_but_not_sales_live_at(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    source_event_id = await _create_event(client, slug="source-event")
    target_event_id = await _create_event(client, slug="target-event")
    await client.put(
        f"/api/v1/events/{source_event_id}/config",
        json={
            "smtp_host": "source-smtp.example",
            "smtp_port": 587,
            "sender_name": "Source Sender",
            "sender_email": "source@example.test",
            "invoice_company_name": "Source Company",
            "invoice_number_prefix": "SRC-",
            "enabled_payment_methods": ["mollie", "door"],
            "sales_live_at": "2026-01-01T00:00:00Z",
        },
    )

    response = await client.post(f"/api/v1/events/{target_event_id}/config/copy-from/{source_event_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["smtp_host"] == "source-smtp.example"
    assert body["smtp_port"] == 587
    assert body["sender_name"] == "Source Sender"
    assert body["invoice_company_name"] == "Source Company"
    assert body["invoice_number_prefix"] == "SRC-"
    assert set(body["enabled_payment_methods"]) == {"mollie", "door"}
    # sales_live_at is deliberately excluded from _COPYABLE_FIELDS.
    assert body["sales_live_at"] is None


async def test_copy_config_copies_secrets_too(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], db_session: AsyncSession
) -> None:
    await _login(client, await make_admin_user())
    source_event_id = await _create_event(client, slug="source-event-secrets")
    target_event_id = await _create_event(client, slug="target-event-secrets")
    await client.put(
        f"/api/v1/events/{source_event_id}/config", json={"smtp_password": "copied-secret-password"}
    )

    response = await client.post(f"/api/v1/events/{target_event_id}/config/copy-from/{source_event_id}")

    assert response.status_code == 200
    assert response.json()["smtp_password_is_set"] is True
    # And it isn't merely "marked set" without actually being usable — the
    # underlying DB row genuinely holds the (re-encrypted-on-read/write, via
    # the ORM) copied ciphertext.
    result = await db_session.execute(
        text("SELECT smtp_password FROM event_configs WHERE id = :id"), {"id": response.json()["id"]}
    )
    assert result.scalar_one() is not None


async def test_copy_config_onto_an_event_that_already_has_a_config_overwrites_it(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    source_event_id = await _create_event(client, slug="source-event-overwrite")
    target_event_id = await _create_event(client, slug="target-event-overwrite")
    await client.put(f"/api/v1/events/{source_event_id}/config", json={"smtp_host": "source-host"})
    await client.put(f"/api/v1/events/{target_event_id}/config", json={"smtp_host": "target-original-host"})

    response = await client.post(f"/api/v1/events/{target_event_id}/config/copy-from/{source_event_id}")

    assert response.status_code == 200
    assert response.json()["smtp_host"] == "source-host"


async def test_copy_config_onto_self_is_rejected(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    event_id = await _create_event(client)
    await client.put(f"/api/v1/events/{event_id}/config", json={"smtp_host": "some-host"})

    response = await client.post(f"/api/v1/events/{event_id}/config/copy-from/{event_id}")

    assert response.status_code == 400


async def test_copy_config_from_a_source_with_no_config_returns_404(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    source_event_id = await _create_event(client, slug="source-no-config")
    target_event_id = await _create_event(client, slug="target-no-config")

    response = await client.post(f"/api/v1/events/{target_event_id}/config/copy-from/{source_event_id}")

    assert response.status_code == 404

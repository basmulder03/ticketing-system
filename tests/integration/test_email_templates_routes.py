"""Integration tests for ``/api/v1/events/{event_id}/email-templates``
(Milestone 4): CRUD happy path, per-(type, language) uniqueness, unknown-
type/language handling, the draft-values preview endpoint, and audit
logging. Admin/agent access-scoping itself (``require_admin_or_agent``) is
covered centrally in ``tests/integration/test_deps_content_scoping.py``,
mirroring Event/Show/TicketType/Theme's existing convention — this file
covers what the routes actually DO once past that gate.
"""

from collections.abc import Awaitable, Callable

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLogEntry
from app.models.enums import EmailTemplateType
from app.models.event import Event
from tests.integration.conftest import SeededAdmin

_TEMPLATE_TYPE = EmailTemplateType.ORDER_CONFIRMATION_TICKET.value


async def _login(client: AsyncClient, seeded: SeededAdmin) -> None:
    response = await client.post(
        "/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password}
    )
    assert response.status_code == 200


async def _admin_client(client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]) -> None:
    await _login(client, await make_admin_user())


async def _action_count(db_session: AsyncSession, action: str, target_id: str) -> int:
    result = await db_session.execute(
        select(func.count())
        .select_from(AuditLogEntry)
        .where(AuditLogEntry.action == action)
        .where(AuditLogEntry.target_id == target_id)
    )
    return result.scalar_one()


# --- CRUD happy path ---------------------------------------------------------


async def test_list_is_empty_for_a_freshly_created_event(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
) -> None:
    await _admin_client(client, make_admin_user)
    event = await make_event()

    response = await client.get(f"/api/v1/events/{event.id}/email-templates")
    assert response.status_code == 200
    assert response.json() == []


async def test_get_uncustomized_template_returns_404_with_clarifying_message(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
) -> None:
    await _admin_client(client, make_admin_user)
    event = await make_event()

    response = await client.get(f"/api/v1/events/{event.id}/email-templates/{_TEMPLATE_TYPE}/en")
    assert response.status_code == 404
    assert "default" in response.json()["detail"].lower()


async def test_put_creates_a_new_template_and_records_a_create_audit_entry(
    client: AsyncClient,
    db_session: AsyncSession,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
) -> None:
    await _admin_client(client, make_admin_user)
    event = await make_event()

    response = await client.put(
        f"/api/v1/events/{event.id}/email-templates/{_TEMPLATE_TYPE}/en",
        json={"subject": "Your tickets for {{event_name}}", "body": "<p>Hi {{buyer_name}}</p>"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["subject"] == "Your tickets for {{event_name}}"
    assert body["body"] == "<p>Hi {{buyer_name}}</p>"
    assert body["language"] == "en"
    assert body["template_type"] == _TEMPLATE_TYPE

    assert await _action_count(db_session, "email_template.create", body["id"]) == 1
    assert await _action_count(db_session, "email_template.update", body["id"]) == 0


async def test_put_again_for_the_same_type_language_updates_in_place_not_duplicates(
    client: AsyncClient,
    db_session: AsyncSession,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
) -> None:
    await _admin_client(client, make_admin_user)
    event = await make_event()

    first = await client.put(
        f"/api/v1/events/{event.id}/email-templates/{_TEMPLATE_TYPE}/en",
        json={"subject": "First subject", "body": "<p>First</p>"},
    )
    assert first.status_code == 200
    template_id = first.json()["id"]

    second = await client.put(
        f"/api/v1/events/{event.id}/email-templates/{_TEMPLATE_TYPE}/en",
        json={"subject": "Second subject", "body": "<p>Second</p>"},
    )
    assert second.status_code == 200
    assert second.json()["id"] == template_id
    assert second.json()["subject"] == "Second subject"

    list_response = await client.get(f"/api/v1/events/{event.id}/email-templates")
    assert len(list_response.json()) == 1

    assert await _action_count(db_session, "email_template.create", template_id) == 1
    assert await _action_count(db_session, "email_template.update", template_id) == 1


async def test_en_and_nl_are_independent_rows_for_the_same_template_type(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _admin_client(client, make_admin_user)
    event = await make_event()

    await client.put(
        f"/api/v1/events/{event.id}/email-templates/{_TEMPLATE_TYPE}/en",
        json={"subject": "EN subject", "body": "<p>EN</p>"},
    )
    await client.put(
        f"/api/v1/events/{event.id}/email-templates/{_TEMPLATE_TYPE}/nl",
        json={"subject": "NL subject", "body": "<p>NL</p>"},
    )

    en_response = await client.get(f"/api/v1/events/{event.id}/email-templates/{_TEMPLATE_TYPE}/en")
    nl_response = await client.get(f"/api/v1/events/{event.id}/email-templates/{_TEMPLATE_TYPE}/nl")
    assert en_response.json()["subject"] == "EN subject"
    assert nl_response.json()["subject"] == "NL subject"

    list_response = await client.get(f"/api/v1/events/{event.id}/email-templates")
    assert len(list_response.json()) == 2


async def test_get_after_put_returns_the_saved_template(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _admin_client(client, make_admin_user)
    event = await make_event()
    await client.put(
        f"/api/v1/events/{event.id}/email-templates/{_TEMPLATE_TYPE}/en",
        json={"subject": "Subject", "body": "<p>Body</p>"},
    )

    response = await client.get(f"/api/v1/events/{event.id}/email-templates/{_TEMPLATE_TYPE}/en")
    assert response.status_code == 200
    assert response.json()["subject"] == "Subject"


async def test_delete_removes_the_customization_and_reverts_to_404(
    client: AsyncClient,
    db_session: AsyncSession,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
) -> None:
    await _admin_client(client, make_admin_user)
    event = await make_event()
    put_response = await client.put(
        f"/api/v1/events/{event.id}/email-templates/{_TEMPLATE_TYPE}/en",
        json={"subject": "Subject", "body": "<p>Body</p>"},
    )
    template_id = put_response.json()["id"]

    delete_response = await client.delete(f"/api/v1/events/{event.id}/email-templates/{_TEMPLATE_TYPE}/en")
    assert delete_response.status_code == 204

    get_response = await client.get(f"/api/v1/events/{event.id}/email-templates/{_TEMPLATE_TYPE}/en")
    assert get_response.status_code == 404

    assert await _action_count(db_session, "email_template.delete", template_id) == 1


async def test_delete_uncustomized_template_returns_404(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _admin_client(client, make_admin_user)
    event = await make_event()

    response = await client.delete(f"/api/v1/events/{event.id}/email-templates/{_TEMPLATE_TYPE}/en")
    assert response.status_code == 404


# --- Unknown type/language handling ------------------------------------------


async def test_unknown_template_type_returns_404(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _admin_client(client, make_admin_user)
    event = await make_event()

    response = await client.get(f"/api/v1/events/{event.id}/email-templates/not_a_real_type/en")
    assert response.status_code == 404


async def test_unsupported_language_returns_404(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _admin_client(client, make_admin_user)
    event = await make_event()

    response = await client.get(f"/api/v1/events/{event.id}/email-templates/{_TEMPLATE_TYPE}/fr")
    assert response.status_code == 404


async def test_get_returns_404_for_unknown_event(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _admin_client(client, make_admin_user)
    response = await client.get(f"/api/v1/events/00000000-0000-0000-0000-000000000000/email-templates/{_TEMPLATE_TYPE}/en")
    assert response.status_code == 404


async def test_put_rejects_missing_body_field(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _admin_client(client, make_admin_user)
    event = await make_event()

    response = await client.put(
        f"/api/v1/events/{event.id}/email-templates/{_TEMPLATE_TYPE}/en", json={"subject": "Only subject"}
    )
    assert response.status_code == 422


# --- Preview endpoint ---------------------------------------------------------


async def test_preview_renders_sample_data_and_does_not_persist(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _admin_client(client, make_admin_user)
    event = await make_event()

    response = await client.post(
        f"/api/v1/events/{event.id}/email-templates/preview",
        json={"language": "en", "subject": "Preview for {{event_name}}", "body": "<p>Hi {{buyer_name}}</p>"},
    )
    assert response.status_code == 200
    body = response.json()
    assert "Preview for" in body["subject"]
    assert "Jamie Sample" in body["html_body"]  # the built-in sample buyer name
    assert body["text_body"]

    # Nothing was persisted.
    list_response = await client.get(f"/api/v1/events/{event.id}/email-templates")
    assert list_response.json() == []


async def test_preview_rejects_an_unsupported_language(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _admin_client(client, make_admin_user)
    event = await make_event()

    response = await client.post(
        f"/api/v1/events/{event.id}/email-templates/preview",
        json={"language": "fr", "subject": "Subject", "body": "<p>Body</p>"},
    )
    assert response.status_code == 422


async def test_preview_trust_boundary_body_html_is_trusted_but_placeholder_values_are_escaped(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    """Documents the actual trust boundary (see ``app.services.
    email_placeholders`` module docstring): ``body`` itself is
    admin/agent-authored HTML CONTENT (same trust level as the rest of the
    email shell this project already trusts its own admin/agent principals
    to author) and is never re-escaped — a literal ``<em>`` typed by an
    admin renders as real emphasis, not literal angle brackets. Only
    placeholder VALUES substituted into it (e.g. the sample ``buyer_name``)
    are escaped. If a future change accidentally started escaping the
    template's own literal HTML, this test would catch that regression."""
    await _admin_client(client, make_admin_user)
    event = await make_event()

    response = await client.post(
        f"/api/v1/events/{event.id}/email-templates/preview",
        json={"language": "en", "subject": "Subject", "body": "<p>Hi {{buyer_name}}</p><em>emphasis</em>"},
    )
    assert response.status_code == 200
    html_body = response.json()["html_body"]
    assert "<em>emphasis</em>" in html_body, "admin-authored literal HTML in body is trusted content, not escaped"
    assert "Jamie Sample" in html_body, "the sample buyer_name placeholder value is substituted in"

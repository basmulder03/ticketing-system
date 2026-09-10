"""Integration tests for ``/api/v1/events`` CRUD, authenticated as a real
admin, against a real DB.

Non-admin/non-agent access to these routes is covered in
``test_deps_content_scoping.py`` — this file focuses on what the routes
actually do: slug uniqueness/pattern validation, draft/published status,
the ``sales_paused`` toggle, and (via ``test_cascade_delete.py``) cascade
delete of everything under an Event.
"""

from collections.abc import Awaitable, Callable

from httpx import AsyncClient

from app.models.enums import PublishStatus
from tests.integration.conftest import SeededAdmin, SeededAgent


async def _login(client: AsyncClient, seeded: SeededAdmin) -> None:
    response = await client.post("/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password})
    assert response.status_code == 200


async def test_create_event_defaults_to_draft_and_unpaused(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())

    response = await client.post("/api/v1/events", json={"name": "Christmas Passion", "slug": "christmas-passion"})

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Christmas Passion"
    assert body["slug"] == "christmas-passion"
    assert body["status"] == "draft"
    assert body["sales_paused"] is False
    assert body["description"] is None


async def test_create_event_with_explicit_status_and_description(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())

    response = await client.post(
        "/api/v1/events",
        json={
            "name": "Summer Concert",
            "slug": "summer-concert",
            "description": "An outdoor summer concert.",
            "status": "published",
            "sales_paused": True,
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "published"
    assert body["sales_paused"] is True
    assert body["description"] == "An outdoor summer concert."


async def test_create_event_duplicate_slug_returns_409(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    first = await client.post("/api/v1/events", json={"name": "Event A", "slug": "duplicate-slug"})
    assert first.status_code == 201

    second = await client.post("/api/v1/events", json={"name": "Event B", "slug": "duplicate-slug"})

    assert second.status_code == 409


async def test_update_event_to_a_slug_already_used_by_another_event_returns_409(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    await client.post("/api/v1/events", json={"name": "Event A", "slug": "event-a"})
    second = await client.post("/api/v1/events", json={"name": "Event B", "slug": "event-b"})
    second_id = second.json()["id"]

    response = await client.patch(f"/api/v1/events/{second_id}", json={"slug": "event-a"})

    assert response.status_code == 409


async def test_update_event_keeping_its_own_slug_does_not_409(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    created = await client.post("/api/v1/events", json={"name": "Event A", "slug": "event-a"})
    event_id = created.json()["id"]

    response = await client.patch(f"/api/v1/events/{event_id}", json={"slug": "event-a", "name": "Event A Renamed"})

    assert response.status_code == 200
    assert response.json()["name"] == "Event A Renamed"


async def test_create_event_rejects_invalid_slug_shape(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())

    # Uppercase, spaces, and leading/trailing hyphens are all outside the
    # slug pattern (``^[a-z0-9]+(?:-[a-z0-9]+)*$`` — see app/schemas/event.py).
    for bad_slug in ("Christmas Passion", "christmas_passion", "-christmas-passion", "christmas-passion-", "UPPER"):
        response = await client.post("/api/v1/events", json={"name": "Event", "slug": bad_slug})
        assert response.status_code == 422, f"expected 422 for slug={bad_slug!r}, got {response.status_code}"


async def test_update_event_rejects_invalid_slug_shape(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    created = await client.post("/api/v1/events", json={"name": "Event", "slug": "valid-slug"})
    event_id = created.json()["id"]

    response = await client.patch(f"/api/v1/events/{event_id}", json={"slug": "Not Valid"})

    assert response.status_code == 422


async def test_get_event_returns_404_for_unknown_id(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())

    response = await client.get("/api/v1/events/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404


async def test_get_event_returns_404_for_malformed_id(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())

    response = await client.get("/api/v1/events/not-a-uuid")

    assert response.status_code == 404


async def test_list_events_returns_newest_first(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    await client.post("/api/v1/events", json={"name": "First", "slug": "first-event"})
    await client.post("/api/v1/events", json={"name": "Second", "slug": "second-event"})

    response = await client.get("/api/v1/events")

    assert response.status_code == 200
    slugs = [event["slug"] for event in response.json()]
    assert slugs == ["second-event", "first-event"]


async def test_update_event_status_transitions_draft_to_published(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    created = await client.post("/api/v1/events", json={"name": "Event", "slug": "status-test"})
    event_id = created.json()["id"]
    assert created.json()["status"] == "draft"

    response = await client.patch(f"/api/v1/events/{event_id}", json={"status": "published"})

    assert response.status_code == 200
    assert response.json()["status"] == "published"


async def test_update_event_status_transitions_published_back_to_draft(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    created = await client.post(
        "/api/v1/events", json={"name": "Event", "slug": "status-test-2", "status": "published"}
    )
    event_id = created.json()["id"]

    response = await client.patch(f"/api/v1/events/{event_id}", json={"status": "draft"})

    assert response.status_code == 200
    assert response.json()["status"] == "draft"


async def test_update_event_sales_paused_toggle(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    created = await client.post("/api/v1/events", json={"name": "Event", "slug": "pause-test"})
    event_id = created.json()["id"]
    assert created.json()["sales_paused"] is False

    paused = await client.patch(f"/api/v1/events/{event_id}", json={"sales_paused": True})
    assert paused.status_code == 200
    assert paused.json()["sales_paused"] is True

    resumed = await client.patch(f"/api/v1/events/{event_id}", json={"sales_paused": False})
    assert resumed.status_code == 200
    assert resumed.json()["sales_paused"] is False


async def test_partial_update_leaves_omitted_fields_unchanged(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    created = await client.post(
        "/api/v1/events",
        json={"name": "Event", "slug": "partial-test", "description": "Original description", "sales_paused": True},
    )
    event_id = created.json()["id"]

    response = await client.patch(f"/api/v1/events/{event_id}", json={"name": "Renamed Event"})

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Renamed Event"
    # Not present in the PATCH body -> untouched, per apply_partial_update's
    # exclude_unset semantics.
    assert body["description"] == "Original description"
    assert body["sales_paused"] is True


async def test_delete_event_returns_204_and_it_is_gone(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    created = await client.post("/api/v1/events", json={"name": "Event", "slug": "delete-test"})
    event_id = created.json()["id"]

    response = await client.delete(f"/api/v1/events/{event_id}")
    assert response.status_code == 204

    follow_up = await client.get(f"/api/v1/events/{event_id}")
    assert follow_up.status_code == 404


async def test_create_event_status_enum_out_of_publish_status(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    """Positive control confirming ``PublishStatus`` is the DRAFT/PUBLISHED
    enum used elsewhere (Show shares this same enum) — not a coincidence of
    matching string values."""
    await _login(client, await make_admin_user())

    response = await client.post(
        "/api/v1/events", json={"name": "Event", "slug": "enum-check", "status": PublishStatus.PUBLISHED.value}
    )

    assert response.status_code == 201
    assert response.json()["status"] == PublishStatus.PUBLISHED.value


# --- preview_token: included for both admin and agent principals ---------
#
# Milestone 2: `preview_token` was added to `EventOut` so the backoffice
# can render a copy-preview-link button (see
# `tests/integration/test_web_backoffice_routes.py`). It's a capability URL,
# not a payment/SMTP-class secret, so both admin and agent principals see
# it — same access tier as every other Event field.


async def test_get_event_includes_preview_token_for_admin(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    created = await client.post("/api/v1/events", json={"name": "Event", "slug": "preview-token-admin"})
    event_id = created.json()["id"]
    assert created.json()["preview_token"]

    response = await client.get(f"/api/v1/events/{event_id}")
    assert response.status_code == 200
    assert response.json()["preview_token"] == created.json()["preview_token"]


async def test_list_events_includes_preview_token_for_admin(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    await client.post("/api/v1/events", json={"name": "Event", "slug": "preview-token-list"})

    response = await client.get("/api/v1/events")
    assert response.status_code == 200
    assert all(event["preview_token"] for event in response.json())


async def test_get_and_list_events_include_preview_token_for_agent(
    client: AsyncClient,
    client_factory: Callable[[str | None], AsyncClient],
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_agent_account: Callable[..., Awaitable[SeededAgent]],
) -> None:
    await _login(client, await make_admin_user())
    created = await client.post("/api/v1/events", json={"name": "Event", "slug": "preview-token-agent"})
    event_id = created.json()["id"]

    agent = await make_agent_account()
    headers = {"X-Agent-Api-Key": agent.raw_key}

    # A fresh, unauthenticated client (no admin session cookie) — the
    # admin-session-cookie path in `get_current_principal` is tried first,
    # so reusing the already-logged-in `client` here would silently resolve
    # as the admin, not actually exercise the agent auth path.
    async with client_factory(None) as agent_client:
        get_response = await agent_client.get(f"/api/v1/events/{event_id}", headers=headers)
        assert get_response.status_code == 200
        assert get_response.json()["preview_token"] == created.json()["preview_token"]

        list_response = await agent_client.get("/api/v1/events", headers=headers)
        assert list_response.status_code == 200
        assert all(event["preview_token"] for event in list_response.json())


# --- Default event ------------------------------------------------------------


async def test_set_default_event_marks_it_and_new_events_default_to_false(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    created = await client.post("/api/v1/events", json={"name": "Event", "slug": "default-event-basic"})
    assert created.json()["is_default_event"] is False
    event_id = created.json()["id"]

    response = await client.post(f"/api/v1/events/{event_id}/set-default")

    assert response.status_code == 200, response.text
    assert response.json()["is_default_event"] is True

    fetched = await client.get(f"/api/v1/events/{event_id}")
    assert fetched.json()["is_default_event"] is True


async def test_setting_a_new_default_event_clears_the_previous_one(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    first = await client.post("/api/v1/events", json={"name": "First", "slug": "default-event-first"})
    second = await client.post("/api/v1/events", json={"name": "Second", "slug": "default-event-second"})
    first_id, second_id = first.json()["id"], second.json()["id"]

    await client.post(f"/api/v1/events/{first_id}/set-default")
    response = await client.post(f"/api/v1/events/{second_id}/set-default")

    assert response.status_code == 200
    assert response.json()["is_default_event"] is True

    first_after = await client.get(f"/api/v1/events/{first_id}")
    assert first_after.json()["is_default_event"] is False


async def test_unset_default_event_clears_it_and_is_idempotent(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    created = await client.post("/api/v1/events", json={"name": "Event", "slug": "default-event-unset"})
    event_id = created.json()["id"]
    await client.post(f"/api/v1/events/{event_id}/set-default")

    first = await client.post(f"/api/v1/events/{event_id}/unset-default")
    assert first.status_code == 200
    assert first.json()["is_default_event"] is False

    # Calling it again on an Event that isn't the default is a safe no-op.
    second = await client.post(f"/api/v1/events/{event_id}/unset-default")
    assert second.status_code == 200
    assert second.json()["is_default_event"] is False


async def test_set_default_event_returns_404_for_unknown_event(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())

    response = await client.post("/api/v1/events/00000000-0000-0000-0000-000000000000/set-default")

    assert response.status_code == 404

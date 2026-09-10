"""The mirror-image guarantee introduced by Milestone 1's
``require_admin_or_agent`` (see ``app/api/deps.py``): agents can now reach
some routes, not just "always rejected everywhere" (that's still covered by
``test_deps_admin_scoping.py`` for the ``require_admin``-only routes).

Two distinct access surfaces are asserted here, across every route in
Event/Show/TicketType/EventConfig:

- Event/Show/TicketType routes (content-type data) use
  ``require_admin_or_agent``: a real ``admin``-role human AND any active
  agent key can reach them; a ``scanner``-role human and an unauthenticated
  caller cannot.
- EventConfig routes (SMTP/Mollie credentials, financial data — including
  the connection-test and copy-config actions) use ``require_admin`` only:
  a real admin can reach them, but an agent key gets 403 on every single
  one, same as a scanner-role human or an unauthenticated caller. This is
  the concrete enforcement of PROJECT_BRIEF.md's "agent keys ... cannot
  touch payment credentials, SMTP credentials ... or financial data".

If a future route is added to any of these four routers, add it to the
relevant list below so this test keeps covering the full set.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import pytest
from httpx import AsyncClient

from app.models.enums import AdminRole, EmailTemplateType
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.show import Show
from app.models.ticket_type import TicketType
from tests.integration.conftest import SeededAdmin, SeededAgent

_TEMPLATE_TYPE = EmailTemplateType.ORDER_CONFIRMATION_TICKET.value

AGENT_API_KEY_HEADER = "X-Agent-Api-Key"


@dataclass(frozen=True)
class ContentTree:
    """IDs of a freshly created Event -> Show -> TicketType chain, plus a
    second ("source") Event with its own EventConfig for the copy-from
    action — everything a route spec's path template might reference."""

    event_id: str
    show_id: str
    ticket_type_id: str
    source_event_id: str


@pytest.fixture
def content_tree_factory(
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> Callable[[], Awaitable[ContentTree]]:
    async def _make() -> ContentTree:
        event = await make_event()
        show = await make_show(event_id=event.id)
        ticket_type = await make_ticket_type(show_id=show.id)
        source_event = await make_event()
        await make_event_config(event_id=source_event.id)
        return ContentTree(
            event_id=str(event.id),
            show_id=str(show.id),
            ticket_type_id=str(ticket_type.id),
            source_event_id=str(source_event.id),
        )

    return _make


# (label, method, path_template, json_body) — path_template is filled via
# ``str.format(**tree.__dict__)``; unused placeholders are simply ignored.
_SHOW_CREATE_BODY = {
    "date": "2026-12-18",
    "doors_time": "19:30:00",
    "start_time": "20:00:00",
    "venue_name": "New Venue",
    "venue_address": "1 New Street",
    "capacity": 50,
}
_TICKET_TYPE_CREATE_BODY = {"name": "New Ticket Type", "price": "12.50", "quantity_available": 20}

CONTENT_GATED_ROUTES: list[tuple[str, str, str, dict[str, object] | None]] = [
    # Event routes
    ("create_event", "POST", "/api/v1/events", {"name": "New Event", "slug": "scoping-test-event"}),
    ("list_events", "GET", "/api/v1/events", None),
    ("get_event", "GET", "/api/v1/events/{event_id}", None),
    ("update_event", "PATCH", "/api/v1/events/{event_id}", {"description": "updated"}),
    ("set_default_event", "POST", "/api/v1/events/{event_id}/set-default", None),
    ("unset_default_event", "POST", "/api/v1/events/{event_id}/unset-default", None),
    ("delete_event", "DELETE", "/api/v1/events/{event_id}", None),
    # Show routes
    ("create_show", "POST", "/api/v1/events/{event_id}/shows", _SHOW_CREATE_BODY),
    ("list_shows", "GET", "/api/v1/events/{event_id}/shows", None),
    ("get_show", "GET", "/api/v1/events/{event_id}/shows/{show_id}", None),
    (
        "update_show",
        "PATCH",
        "/api/v1/events/{event_id}/shows/{show_id}",
        {"venue_name": "Updated Venue"},
    ),
    ("delete_show", "DELETE", "/api/v1/events/{event_id}/shows/{show_id}", None),
    # TicketType routes
    ("create_ticket_type", "POST", "/api/v1/shows/{show_id}/ticket-types", _TICKET_TYPE_CREATE_BODY),
    ("list_ticket_types", "GET", "/api/v1/shows/{show_id}/ticket-types", None),
    ("get_ticket_type", "GET", "/api/v1/shows/{show_id}/ticket-types/{ticket_type_id}", None),
    (
        "update_ticket_type",
        "PATCH",
        "/api/v1/shows/{show_id}/ticket-types/{ticket_type_id}",
        {"name": "Updated"},
    ),
    ("delete_ticket_type", "DELETE", "/api/v1/shows/{show_id}/ticket-types/{ticket_type_id}", None),
    # EmailTemplate routes (Milestone 4) — "email template content" is
    # explicitly listed as agent-accessible content-type data in
    # PROJECT_BRIEF.md's AI/Agent Access section.
    ("list_email_templates", "GET", "/api/v1/events/{event_id}/email-templates", None),
    (
        "get_email_template",
        "GET",
        f"/api/v1/events/{{event_id}}/email-templates/{_TEMPLATE_TYPE}/en",
        None,
    ),
    (
        "upsert_email_template",
        "PUT",
        f"/api/v1/events/{{event_id}}/email-templates/{_TEMPLATE_TYPE}/en",
        {"subject": "Subject", "body": "<p>Body</p>"},
    ),
    (
        "preview_email_template",
        "POST",
        "/api/v1/events/{event_id}/email-templates/preview",
        {"language": "en", "subject": "Subject", "body": "<p>Body</p>"},
    ),
    (
        "delete_email_template",
        "DELETE",
        f"/api/v1/events/{{event_id}}/email-templates/{_TEMPLATE_TYPE}/en",
        None,
    ),
]
_CONTENT_IDS = [route[0] for route in CONTENT_GATED_ROUTES]

CONFIG_GATED_ROUTES: list[tuple[str, str, str, dict[str, object] | None]] = [
    ("get_config", "GET", "/api/v1/events/{event_id}/config", None),
    ("put_config", "PUT", "/api/v1/events/{event_id}/config", {"smtp_host": "mailpit"}),
    (
        "test_email",
        "POST",
        "/api/v1/events/{event_id}/config/test-email",
        {"recipient": "test@example.test"},
    ),
    ("test_mollie", "POST", "/api/v1/events/{event_id}/config/test-mollie", {"environment": "test"}),
    (
        "copy_from",
        "POST",
        "/api/v1/events/{event_id}/config/copy-from/{source_event_id}",
        None,
    ),
]
_CONFIG_IDS = [route[0] for route in CONFIG_GATED_ROUTES]


async def _request(
    client: AsyncClient,
    method: str,
    path_template: str,
    json_body: dict[str, object] | None,
    tree: ContentTree,
) -> int:
    path = path_template.format(
        event_id=tree.event_id,
        show_id=tree.show_id,
        ticket_type_id=tree.ticket_type_id,
        source_event_id=tree.source_event_id,
    )
    response = await client.request(method, path, json=json_body)
    return response.status_code


# --- Content routes (require_admin_or_agent): admin + agent both allowed ---


@pytest.mark.parametrize(("label", "method", "path_template", "json_body"), CONTENT_GATED_ROUTES, ids=_CONTENT_IDS)
async def test_admin_can_reach_content_gated_routes(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    content_tree_factory: Callable[[], Awaitable[ContentTree]],
    label: str,
    method: str,
    path_template: str,
    json_body: dict[str, object] | None,
) -> None:
    seeded = await make_admin_user(role=AdminRole.ADMIN)
    login = await client.post("/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password})
    assert login.status_code == 200
    tree = await content_tree_factory()

    status_code = await _request(client, method, path_template, json_body, tree)

    assert status_code not in (401, 403)


@pytest.mark.parametrize(("label", "method", "path_template", "json_body"), CONTENT_GATED_ROUTES, ids=_CONTENT_IDS)
async def test_agent_can_reach_content_gated_routes(
    client: AsyncClient,
    make_agent_account: Callable[..., Awaitable[SeededAgent]],
    content_tree_factory: Callable[[], Awaitable[ContentTree]],
    label: str,
    method: str,
    path_template: str,
    json_body: dict[str, object] | None,
) -> None:
    seeded = await make_agent_account()
    client.headers[AGENT_API_KEY_HEADER] = seeded.raw_key
    tree = await content_tree_factory()

    status_code = await _request(client, method, path_template, json_body, tree)

    # An agent principal must reach Event/Show/TicketType routes exactly
    # like an admin does — this is the concrete "agents CAN reach content
    # data" half of the mirror-image guarantee.
    assert status_code not in (401, 403)


@pytest.mark.parametrize(("label", "method", "path_template", "json_body"), CONTENT_GATED_ROUTES, ids=_CONTENT_IDS)
async def test_scanner_role_admin_cannot_reach_content_gated_routes(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    content_tree_factory: Callable[[], Awaitable[ContentTree]],
    label: str,
    method: str,
    path_template: str,
    json_body: dict[str, object] | None,
) -> None:
    seeded = await make_admin_user(role=AdminRole.SCANNER)
    login = await client.post("/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password})
    assert login.status_code == 200
    tree = await content_tree_factory()

    status_code = await _request(client, method, path_template, json_body, tree)

    assert status_code == 403


@pytest.mark.parametrize(("label", "method", "path_template", "json_body"), CONTENT_GATED_ROUTES, ids=_CONTENT_IDS)
async def test_unauthenticated_caller_cannot_reach_content_gated_routes(
    client: AsyncClient,
    content_tree_factory: Callable[[], Awaitable[ContentTree]],
    label: str,
    method: str,
    path_template: str,
    json_body: dict[str, object] | None,
) -> None:
    tree = await content_tree_factory()

    status_code = await _request(client, method, path_template, json_body, tree)

    assert status_code == 401


# --- EventConfig routes (require_admin only): agents excluded everywhere ---


@pytest.mark.parametrize(("label", "method", "path_template", "json_body"), CONFIG_GATED_ROUTES, ids=_CONFIG_IDS)
async def test_admin_can_reach_config_gated_routes(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    content_tree_factory: Callable[[], Awaitable[ContentTree]],
    label: str,
    method: str,
    path_template: str,
    json_body: dict[str, object] | None,
) -> None:
    seeded = await make_admin_user(role=AdminRole.ADMIN)
    login = await client.post("/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password})
    assert login.status_code == 200
    tree = await content_tree_factory()

    status_code = await _request(client, method, path_template, json_body, tree)

    assert status_code not in (401, 403)


@pytest.mark.parametrize(("label", "method", "path_template", "json_body"), CONFIG_GATED_ROUTES, ids=_CONFIG_IDS)
async def test_agent_cannot_reach_config_gated_routes(
    client: AsyncClient,
    make_agent_account: Callable[..., Awaitable[SeededAgent]],
    content_tree_factory: Callable[[], Awaitable[ContentTree]],
    label: str,
    method: str,
    path_template: str,
    json_body: dict[str, object] | None,
) -> None:
    seeded = await make_agent_account()
    client.headers[AGENT_API_KEY_HEADER] = seeded.raw_key
    tree = await content_tree_factory()

    status_code = await _request(client, method, path_template, json_body, tree)

    # The core guarantee this file exists to prove: EventConfig (SMTP/Mollie
    # credentials, invoice/financial details) is unreachable with an agent
    # key, on every single route including the connection-test and
    # copy-config actions, no exceptions.
    assert status_code == 403


@pytest.mark.parametrize(("label", "method", "path_template", "json_body"), CONFIG_GATED_ROUTES, ids=_CONFIG_IDS)
async def test_scanner_role_admin_cannot_reach_config_gated_routes(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    content_tree_factory: Callable[[], Awaitable[ContentTree]],
    label: str,
    method: str,
    path_template: str,
    json_body: dict[str, object] | None,
) -> None:
    seeded = await make_admin_user(role=AdminRole.SCANNER)
    login = await client.post("/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password})
    assert login.status_code == 200
    tree = await content_tree_factory()

    status_code = await _request(client, method, path_template, json_body, tree)

    assert status_code == 403


@pytest.mark.parametrize(("label", "method", "path_template", "json_body"), CONFIG_GATED_ROUTES, ids=_CONFIG_IDS)
async def test_unauthenticated_caller_cannot_reach_config_gated_routes(
    client: AsyncClient,
    content_tree_factory: Callable[[], Awaitable[ContentTree]],
    label: str,
    method: str,
    path_template: str,
    json_body: dict[str, object] | None,
) -> None:
    tree = await content_tree_factory()

    status_code = await _request(client, method, path_template, json_body, tree)

    assert status_code == 401

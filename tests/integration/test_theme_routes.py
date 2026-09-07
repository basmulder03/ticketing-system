"""Integration tests for ``/api/v1/events/{event_id}/theme*`` (Theme CRUD,
image upload/delete, "duplicate from previous event", and the draft-values
live-preview endpoint), against a real DB.

Covers, per the Milestone 1.5 test-writer brief:
- The admin/agent/scanner/unauthenticated access-scoping matrix, mirroring
  ``test_deps_content_scoping.py``'s pattern for Event/Show/TicketType:
  Theme routes use ``require_admin_or_agent`` (agents CAN reach them),
  same as other content-type data.
- Response shape: no secrets on a Theme (there are none), but logo/
  background paths must come back as full ``/uploads/...`` URLs, never a
  raw filesystem path.
- The preview endpoint routes draft (unsaved) values through the exact
  same sanitizer as the real save path.
- "Duplicate theme from previous event" (copy-from).
- Audit log attribution (human vs. agent) across create/update/upload/
  delete/copy-from.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.css_sanitizer import sanitize_custom_css
from app.models.audit_log import AuditLogEntry
from app.models.enums import AdminRole
from app.models.event import Event
from tests.integration.conftest import SeededAdmin, SeededAgent

AGENT_API_KEY_HEADER = "X-Agent-Api-Key"

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_VALID_PNG_BYTES = _PNG_MAGIC + b"restofpngdata"

_VALID_THEME_BODY: dict[str, object] = {
    "primary_color": "#111111",
    "secondary_color": "#eeeeee",
    "accent_color": "#c9a227",
    "font_choice": "system-sans",
}


async def _login(client: AsyncClient, seeded: SeededAdmin) -> None:
    response = await client.post("/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password})
    assert response.status_code == 200


async def _create_event(client: AsyncClient, slug: str = "theme-test-event") -> str:
    response = await client.post("/api/v1/events", json={"name": "Theme Test Event", "slug": slug})
    assert response.status_code == 201
    return str(response.json()["id"])


async def _entries_for(db_session: AsyncSession, action: str) -> list[AuditLogEntry]:
    result = await db_session.execute(select(AuditLogEntry).where(AuditLogEntry.action == action))
    return list(result.scalars().all())


# --- Access-scoping matrix ---


@dataclass(frozen=True)
class ThemeTree:
    event_id: str
    source_event_id: str


@pytest.fixture
def theme_tree_factory(
    make_event: Callable[..., Awaitable[Event]],
) -> Callable[[], Awaitable[ThemeTree]]:
    async def _make() -> ThemeTree:
        event = await make_event()
        source_event = await make_event()
        return ThemeTree(event_id=str(event.id), source_event_id=str(source_event.id))

    return _make


# (label, method, path_template, json_body) — GET/PUT/preview/copy-from,
# the JSON-body routes. Logo/background upload/delete need multipart and
# are covered separately below.
THEME_GATED_ROUTES: list[tuple[str, str, str, dict[str, object] | None]] = [
    ("get_theme", "GET", "/api/v1/events/{event_id}/theme", None),
    ("put_theme", "PUT", "/api/v1/events/{event_id}/theme", _VALID_THEME_BODY),
    ("preview_theme", "POST", "/api/v1/events/{event_id}/theme/preview", _VALID_THEME_BODY),
    ("copy_theme", "POST", "/api/v1/events/{event_id}/theme/copy-from/{source_event_id}", None),
]
_THEME_IDS = [route[0] for route in THEME_GATED_ROUTES]


async def _request(
    client: AsyncClient, method: str, path_template: str, json_body: dict[str, object] | None, tree: ThemeTree
) -> int:
    path = path_template.format(event_id=tree.event_id, source_event_id=tree.source_event_id)
    response = await client.request(method, path, json=json_body)
    return response.status_code


@pytest.mark.parametrize(("label", "method", "path_template", "json_body"), THEME_GATED_ROUTES, ids=_THEME_IDS)
async def test_admin_can_reach_theme_routes(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    theme_tree_factory: Callable[[], Awaitable[ThemeTree]],
    label: str,
    method: str,
    path_template: str,
    json_body: dict[str, object] | None,
) -> None:
    await _login(client, await make_admin_user(role=AdminRole.ADMIN))
    tree = await theme_tree_factory()
    # copy-from needs a source theme to exist to reach past its own 404.
    if label == "copy_theme":
        await client.put(f"/api/v1/events/{tree.source_event_id}/theme", json=_VALID_THEME_BODY)

    status_code = await _request(client, method, path_template, json_body, tree)

    assert status_code not in (401, 403)


@pytest.mark.parametrize(("label", "method", "path_template", "json_body"), THEME_GATED_ROUTES, ids=_THEME_IDS)
async def test_agent_can_reach_theme_routes(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_agent_account: Callable[..., Awaitable[SeededAgent]],
    theme_tree_factory: Callable[[], Awaitable[ThemeTree]],
    label: str,
    method: str,
    path_template: str,
    json_body: dict[str, object] | None,
) -> None:
    tree = await theme_tree_factory()
    if label == "copy_theme":
        # Seed the source theme as an admin first (own client), then switch
        # this client over to the agent key for the actual request.
        admin_seeded = await make_admin_user(role=AdminRole.ADMIN)
        await _login(client, admin_seeded)
        await client.put(f"/api/v1/events/{tree.source_event_id}/theme", json=_VALID_THEME_BODY)
        await client.post("/api/v1/auth/logout")

    seeded = await make_agent_account()
    client.headers[AGENT_API_KEY_HEADER] = seeded.raw_key

    status_code = await _request(client, method, path_template, json_body, tree)

    # The core "agents CAN reach theme fields" guarantee — theme fields are
    # explicitly named as agent-accessible content in PROJECT_BRIEF.md.
    assert status_code not in (401, 403)


@pytest.mark.parametrize(("label", "method", "path_template", "json_body"), THEME_GATED_ROUTES, ids=_THEME_IDS)
async def test_scanner_role_admin_cannot_reach_theme_routes(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    theme_tree_factory: Callable[[], Awaitable[ThemeTree]],
    label: str,
    method: str,
    path_template: str,
    json_body: dict[str, object] | None,
) -> None:
    await _login(client, await make_admin_user(role=AdminRole.SCANNER))
    tree = await theme_tree_factory()

    status_code = await _request(client, method, path_template, json_body, tree)

    assert status_code == 403


@pytest.mark.parametrize(("label", "method", "path_template", "json_body"), THEME_GATED_ROUTES, ids=_THEME_IDS)
async def test_unauthenticated_caller_cannot_reach_theme_routes(
    client: AsyncClient,
    theme_tree_factory: Callable[[], Awaitable[ThemeTree]],
    label: str,
    method: str,
    path_template: str,
    json_body: dict[str, object] | None,
) -> None:
    tree = await theme_tree_factory()

    status_code = await _request(client, method, path_template, json_body, tree)

    assert status_code == 401


# --- Image upload/delete scoping (multipart, tested separately from the
# JSON-body matrix above) ---


async def _upload_logo(client: AsyncClient, event_id: str) -> int:
    response = await client.put(
        f"/api/v1/events/{event_id}/theme/logo",
        files={"file": ("logo.png", _VALID_PNG_BYTES, "image/png")},
    )
    return response.status_code


async def test_admin_can_upload_and_delete_logo(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user(role=AdminRole.ADMIN))
    event_id = await _create_event(client, slug="logo-admin-event")

    upload_status = await _upload_logo(client, event_id)
    delete_response = await client.delete(f"/api/v1/events/{event_id}/theme/logo")

    assert upload_status == 200
    assert delete_response.status_code == 200


async def test_agent_can_upload_and_delete_background(
    client: AsyncClient, make_agent_account: Callable[..., Awaitable[SeededAgent]]
) -> None:
    # Events routes are also agent-reachable, so an agent key alone can
    # both create the event and manage its theme images end to end.
    seeded = await make_agent_account()
    client.headers[AGENT_API_KEY_HEADER] = seeded.raw_key
    event_id = await _create_event(client, slug="background-agent-event")

    upload_response = await client.put(
        f"/api/v1/events/{event_id}/theme/background",
        files={"file": ("bg.png", _VALID_PNG_BYTES, "image/png")},
    )
    delete_response = await client.delete(f"/api/v1/events/{event_id}/theme/background")

    assert upload_response.status_code == 200
    assert delete_response.status_code == 200


async def test_scanner_cannot_upload_logo(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    event = await make_event()
    await _login(client, await make_admin_user(role=AdminRole.SCANNER))

    status_code = await _upload_logo(client, str(event.id))

    assert status_code == 403


async def test_unauthenticated_cannot_upload_logo(
    client: AsyncClient, make_event: Callable[..., Awaitable[Event]]
) -> None:
    event = await make_event()

    status_code = await _upload_logo(client, str(event.id))

    assert status_code == 401


# --- Response shape: URLs, not raw filesystem paths ---


async def test_logo_url_in_response_is_a_full_uploads_url_not_a_raw_path(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    event_id = await _create_event(client, slug="logo-url-event")

    response = await client.put(
        f"/api/v1/events/{event_id}/theme/logo",
        files={"file": ("logo.png", _VALID_PNG_BYTES, "image/png")},
    )

    assert response.status_code == 200
    logo_url = response.json()["logo_url"]
    assert logo_url.startswith("/uploads/themes/")
    assert "/app/uploads" not in logo_url  # never leak the raw filesystem base path


async def test_logo_url_is_none_when_no_logo_uploaded(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    event_id = await _create_event(client, slug="no-logo-event")

    response = await client.put(f"/api/v1/events/{event_id}/theme", json=_VALID_THEME_BODY)

    assert response.status_code == 200
    assert response.json()["logo_url"] is None
    assert response.json()["background_image_url"] is None


# --- Save path sanitizes before persisting ---


async def test_put_theme_stores_the_sanitized_result_not_the_raw_submission(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    event_id = await _create_event(client, slug="sanitize-save-event")
    raw_css = "body { color: red; } .event-content { color: blue; }"

    response = await client.put(
        f"/api/v1/events/{event_id}/theme", json={**_VALID_THEME_BODY, "custom_css": raw_css}
    )

    assert response.status_code == 200
    assert response.json()["custom_css"] == sanitize_custom_css(raw_css)
    assert "body" not in response.json()["custom_css"]


async def test_put_theme_with_only_adversarial_css_stores_null_and_flips_is_custom_css_active_off(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    event_id = await _create_event(client, slug="fully-stripped-event")

    response = await client.put(
        f"/api/v1/events/{event_id}/theme", json={**_VALID_THEME_BODY, "custom_css": "body { color: red; }"}
    )

    assert response.status_code == 200
    assert response.json()["custom_css"] is None
    assert response.json()["is_custom_css_active"] is False


async def test_put_theme_with_active_custom_css_sets_the_flag(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    event_id = await _create_event(client, slug="active-css-event")

    response = await client.put(
        f"/api/v1/events/{event_id}/theme",
        json={**_VALID_THEME_BODY, "custom_css": ".event-content { color: blue; }"},
    )

    assert response.status_code == 200
    assert response.json()["is_custom_css_active"] is True


# --- Preview endpoint: same sanitizer, never persists ---


async def test_preview_endpoint_sanitizes_identically_to_the_pure_function(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    event_id = await _create_event(client, slug="preview-parity-event")
    draft_css = "body { color: red; } .event-content { position: fixed; color: green; }"

    response = await client.post(
        f"/api/v1/events/{event_id}/theme/preview", json={**_VALID_THEME_BODY, "custom_css": draft_css}
    )

    assert response.status_code == 200
    assert response.json()["sanitized_custom_css"] == sanitize_custom_css(draft_css)


async def test_preview_endpoint_does_not_persist_anything(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    event_id = await _create_event(client, slug="preview-no-persist-event")

    preview = await client.post(
        f"/api/v1/events/{event_id}/theme/preview",
        json={**_VALID_THEME_BODY, "custom_css": ".event-content { color: hotpink; }"},
    )
    assert preview.status_code == 200

    get_response = await client.get(f"/api/v1/events/{event_id}/theme")
    assert get_response.status_code == 404  # no theme was ever saved


async def test_preview_endpoint_reflects_draft_values_independent_of_saved_theme(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    event_id = await _create_event(client, slug="preview-draft-vs-saved-event")
    await client.put(f"/api/v1/events/{event_id}/theme", json=_VALID_THEME_BODY)  # saved: no custom CSS

    preview = await client.post(
        f"/api/v1/events/{event_id}/theme/preview",
        json={**_VALID_THEME_BODY, "custom_css": ".event-content { color: hotpink; }"},
    )

    assert preview.status_code == 200
    assert preview.json()["is_custom_css_active"] is True
    saved = await client.get(f"/api/v1/events/{event_id}/theme")
    assert saved.json()["is_custom_css_active"] is False


# --- "Duplicate theme from previous event" ---


async def test_copy_theme_copies_colors_font_css_and_status_but_not_images(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    source_event_id = await _create_event(client, slug="copy-source-event")
    target_event_id = await _create_event(client, slug="copy-target-event")
    await client.put(
        f"/api/v1/events/{source_event_id}/theme",
        json={
            "primary_color": "#ff0000",
            "secondary_color": "#00ff00",
            "accent_color": "#0000ff",
            "font_choice": "lora",
            "custom_css": ".event-content { color: purple; }",
            "status": "published",
        },
    )
    await client.put(
        f"/api/v1/events/{source_event_id}/theme/logo",
        files={"file": ("logo.png", _VALID_PNG_BYTES, "image/png")},
    )

    response = await client.post(f"/api/v1/events/{target_event_id}/theme/copy-from/{source_event_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["primary_color"] == "#ff0000"
    assert body["secondary_color"] == "#00ff00"
    assert body["accent_color"] == "#0000ff"
    assert body["font_choice"] == "lora"
    assert body["custom_css"] == ".event-content { color: purple; }"
    assert body["status"] == "published"
    assert body["logo_url"] is None  # images deliberately not copied


async def test_copy_theme_onto_self_is_rejected(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    event_id = await _create_event(client, slug="copy-self-event")
    await client.put(f"/api/v1/events/{event_id}/theme", json=_VALID_THEME_BODY)

    response = await client.post(f"/api/v1/events/{event_id}/theme/copy-from/{event_id}")

    assert response.status_code == 400


async def test_copy_theme_from_a_source_with_no_theme_returns_404(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    source_event_id = await _create_event(client, slug="copy-no-theme-source")
    target_event_id = await _create_event(client, slug="copy-no-theme-target")

    response = await client.post(f"/api/v1/events/{target_event_id}/theme/copy-from/{source_event_id}")

    assert response.status_code == 404


# --- Audit log attribution ---


async def test_theme_create_and_update_are_audited_and_attributed_to_the_admin(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], db_session: AsyncSession
) -> None:
    seeded = await make_admin_user()
    await _login(client, seeded)
    event_id = await _create_event(client, slug="audit-theme-event")

    await client.put(f"/api/v1/events/{event_id}/theme", json=_VALID_THEME_BODY)
    await client.put(f"/api/v1/events/{event_id}/theme", json={"accent_color": "#123456"})

    for action in ("theme.create", "theme.update"):
        entries = await _entries_for(db_session, action)
        assert len(entries) == 1, f"expected exactly one {action} entry"
        entry = entries[0]
        assert entry.actor_type.value == "human"
        assert entry.actor_name == seeded.user.email
        assert entry.target_type == "Theme"


async def test_theme_create_is_attributed_to_the_agent_that_made_it(
    client: AsyncClient, make_agent_account: Callable[..., Awaitable[SeededAgent]], db_session: AsyncSession
) -> None:
    seeded = await make_agent_account(name="theme-bot")
    client.headers[AGENT_API_KEY_HEADER] = seeded.raw_key
    event_id = await _create_event(client, slug="audit-theme-agent-event")

    response = await client.put(f"/api/v1/events/{event_id}/theme", json=_VALID_THEME_BODY)
    assert response.status_code == 200

    entries = await _entries_for(db_session, "theme.create")
    assert len(entries) == 1
    entry = entries[0]
    assert entry.actor_type.value == "ai_agent"
    assert entry.actor_id == seeded.account.id
    assert entry.actor_name == "theme-bot"


async def test_theme_logo_upload_and_delete_are_audited(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], db_session: AsyncSession
) -> None:
    await _login(client, await make_admin_user())
    event_id = await _create_event(client, slug="audit-logo-event")
    await client.put(f"/api/v1/events/{event_id}/theme", json=_VALID_THEME_BODY)

    await client.put(
        f"/api/v1/events/{event_id}/theme/logo", files={"file": ("logo.png", _VALID_PNG_BYTES, "image/png")}
    )
    await client.delete(f"/api/v1/events/{event_id}/theme/logo")

    upload_entries = await _entries_for(db_session, "theme.logo.upload")
    delete_entries = await _entries_for(db_session, "theme.logo.delete")
    assert len(upload_entries) == 1
    assert len(delete_entries) == 1


async def test_copy_theme_is_audited(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], db_session: AsyncSession
) -> None:
    seeded = await make_admin_user()
    await _login(client, seeded)
    source_event_id = await _create_event(client, slug="audit-copy-source")
    target_event_id = await _create_event(client, slug="audit-copy-target")
    await client.put(f"/api/v1/events/{source_event_id}/theme", json=_VALID_THEME_BODY)

    response = await client.post(f"/api/v1/events/{target_event_id}/theme/copy-from/{source_event_id}")
    assert response.status_code == 200

    entries = await _entries_for(db_session, "theme.copy_from")
    assert len(entries) == 1
    entry = entries[0]
    assert entry.actor_name == seeded.user.email
    assert entry.detail is not None
    assert entry.detail["source_event_id"] == source_event_id
    assert entry.detail["target_event_id"] == target_event_id


async def test_theme_preview_action_is_not_written_to_the_audit_log(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], db_session: AsyncSession
) -> None:
    await _login(client, await make_admin_user())
    event_id = await _create_event(client, slug="audit-preview-event")

    before = (await db_session.execute(select(AuditLogEntry))).scalars().all()
    await client.post(f"/api/v1/events/{event_id}/theme/preview", json=_VALID_THEME_BODY)
    after = (await db_session.execute(select(AuditLogEntry))).scalars().all()

    assert len(after) == len(before)

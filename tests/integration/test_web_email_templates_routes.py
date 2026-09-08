"""Integration tests for the backoffice EmailTemplate editor web surface
(``app/web/routes/email_templates.py``, Milestone 4): the editor page
(GET), save/reset form handlers, and the ``preview-fragment`` HTMX
endpoint.

Mirrors ``tests/integration/test_web_backoffice_routes.py``'s conventions
(``_api_login`` bypassing the web login form/CSRF, per-page unauthenticated-
redirect tests, CSRF happy/missing/wrong-token tests matching the Theme
editor's own precedent for this exact page shape).
"""

from collections.abc import Awaitable, Callable

from httpx import AsyncClient

from app.models.event import Event
from app.web.csrf import CSRF_COOKIE_NAME
from tests.integration.conftest import SeededAdmin


async def _api_login(client: AsyncClient, seeded: SeededAdmin) -> None:
    response = await client.post(
        "/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password}
    )
    assert response.status_code == 200


async def _get_editor_csrf_token(client: AsyncClient, event_id: str, *, language: str = "en") -> str:
    page = await client.get(f"/events/{event_id}/email-templates?language={language}")
    assert page.status_code == 200
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert token
    return token


# --- Unauthenticated access ------------------------------------------------


async def test_unauthenticated_editor_get_redirects_to_login(
    client: AsyncClient, make_event: Callable[..., Awaitable[Event]]
) -> None:
    event = await make_event()

    response = await client.get(f"/events/{event.id}/email-templates")

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


async def test_unauthenticated_save_redirects_to_login(
    client: AsyncClient, make_event: Callable[..., Awaitable[Event]]
) -> None:
    event = await make_event()

    response = await client.post(
        f"/events/{event.id}/email-templates",
        data={"language": "en", "subject": "x", "body": "<p>x</p>", "csrf_token": "irrelevant"},
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


async def test_unauthenticated_reset_redirects_to_login(
    client: AsyncClient, make_event: Callable[..., Awaitable[Event]]
) -> None:
    event = await make_event()

    response = await client.post(
        f"/events/{event.id}/email-templates/reset",
        data={"language": "en", "csrf_token": "irrelevant"},
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


async def test_unauthenticated_preview_fragment_redirects_to_login(
    client: AsyncClient, make_event: Callable[..., Awaitable[Event]]
) -> None:
    event = await make_event()

    response = await client.post(
        f"/events/{event.id}/email-templates/preview-fragment",
        data={"language": "en", "subject": "x", "body": "<p>x</p>", "csrf_token": "irrelevant"},
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


# --- GET: pre-filled vs built-in default -----------------------------------


async def test_get_shows_built_in_defaults_when_no_template_saved_yet(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event(name="Default Copy Event")

    response = await client.get(f"/events/{event.id}/email-templates?language=en")

    assert response.status_code == 200
    assert "No customized template yet for this language" in response.text
    assert 'value="Your tickets for {{event_name}}"' in response.text
    assert "Reset to built-in default" in response.text
    # The reset button is disabled when there's nothing saved to reset.
    assert "disabled" in response.text


async def test_get_shows_prefilled_form_when_a_template_is_saved(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event(name="Prefilled Event")
    await client.put(
        f"/api/v1/events/{event.id}/email-templates/order_confirmation_ticket/en",
        json={"subject": "Custom subject {{event_name}}", "body": "<p>Custom body {{buyer_name}}</p>"},
    )

    response = await client.get(f"/events/{event.id}/email-templates?language=en")

    assert response.status_code == 200
    assert "No customized template yet for this language" not in response.text
    assert 'value="Custom subject {{event_name}}"' in response.text
    assert "Custom body {{buyer_name}}" in response.text


# --- Save: persists + redirects with success flash -------------------------


async def test_save_persists_and_redirects_with_success_flash(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event(name="Save Event")
    token = await _get_editor_csrf_token(client, str(event.id))

    response = await client.post(
        f"/events/{event.id}/email-templates",
        data={
            "language": "en",
            "subject": "Saved subject {{event_name}}",
            "body": "<p>Saved body</p>",
            "csrf_token": token,
        },
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/events/{event.id}/email-templates?language=en&flash=Email%20template%20saved.&flash_kind=success"

    saved = await client.get(f"/api/v1/events/{event.id}/email-templates/order_confirmation_ticket/en")
    assert saved.status_code == 200
    assert saved.json()["subject"] == "Saved subject {{event_name}}"
    assert saved.json()["body"] == "<p>Saved body</p>"


async def test_save_form_with_missing_csrf_is_rejected(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()

    response = await client.post(
        f"/events/{event.id}/email-templates",
        data={"language": "en", "subject": "x", "body": "<p>x</p>"},
    )

    assert response.status_code == 422


async def test_save_form_with_wrong_csrf_is_rejected_403(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()
    await _get_editor_csrf_token(client, str(event.id))

    response = await client.post(
        f"/events/{event.id}/email-templates",
        data={"language": "en", "subject": "x", "body": "<p>x</p>", "csrf_token": "wrong-token"},
    )

    assert response.status_code == 403


# --- Reset ------------------------------------------------------------------


async def test_reset_deletes_customization_and_redirects_with_success_flash(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event(name="Reset Event")
    await client.put(
        f"/api/v1/events/{event.id}/email-templates/order_confirmation_ticket/en",
        json={"subject": "Custom", "body": "<p>Custom</p>"},
    )
    token = await _get_editor_csrf_token(client, str(event.id))

    response = await client.post(
        f"/events/{event.id}/email-templates/reset", data={"language": "en", "csrf_token": token}
    )

    assert response.status_code == 303
    assert response.headers["location"] == (
        f"/events/{event.id}/email-templates?language=en&flash=Reverted%20to%20the%20built-in%20default%20template.&flash_kind=success"
    )

    saved = await client.get(f"/api/v1/events/{event.id}/email-templates/order_confirmation_ticket/en")
    assert saved.status_code == 404


async def test_reset_form_with_missing_csrf_is_rejected(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()

    response = await client.post(f"/events/{event.id}/email-templates/reset", data={"language": "en"})

    assert response.status_code == 422


async def test_reset_form_with_wrong_csrf_is_rejected_403(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()
    await _get_editor_csrf_token(client, str(event.id))

    response = await client.post(
        f"/events/{event.id}/email-templates/reset", data={"language": "en", "csrf_token": "wrong-token"}
    )

    assert response.status_code == 403


# --- preview-fragment: renders using the event's REAL theme colors --------


async def test_preview_fragment_renders_using_the_events_real_theme_colors(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event(name="Theme Colors Event")
    await client.put(
        f"/api/v1/events/{event.id}/theme",
        json={"primary_color": "#123456", "secondary_color": "#abcdef", "accent_color": "#c9a227"},
    )
    token = await _get_editor_csrf_token(client, str(event.id))

    response = await client.post(
        f"/events/{event.id}/email-templates/preview-fragment",
        data={
            "language": "en",
            "subject": "Preview subject",
            "body": "<p>Preview body</p>",
            "csrf_token": token,
        },
    )

    assert response.status_code == 200
    # The fragment embeds the rendered HTML email inside an iframe's
    # `srcdoc` attribute, so Jinja auto-escapes it — check for the
    # HTML-escaped forms of the inline style values the real theme colors
    # actually land in (see app.services.email_render.
    # render_order_confirmation_email: `background-color:{secondary}` /
    # `color:{primary}`).
    assert "background-color:#abcdef" in response.text
    assert "color:#123456" in response.text
    assert "Preview subject" in response.text


async def test_preview_fragment_missing_csrf_is_rejected(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()

    response = await client.post(
        f"/events/{event.id}/email-templates/preview-fragment",
        data={"language": "en", "subject": "x", "body": "<p>x</p>"},
    )

    assert response.status_code == 422


async def test_preview_fragment_wrong_csrf_is_rejected_403(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()
    await _get_editor_csrf_token(client, str(event.id))

    response = await client.post(
        f"/events/{event.id}/email-templates/preview-fragment",
        data={"language": "en", "subject": "x", "body": "<p>x</p>", "csrf_token": "wrong-token"},
    )

    assert response.status_code == 403


# --- Unknown `language` query param falls back to `en` ---------------------


async def test_get_with_unknown_language_falls_back_to_en(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event(name="Fallback Event")

    response = await client.get(f"/events/{event.id}/email-templates?language=fr")

    assert response.status_code == 200
    # The English tab is the active one, and the English built-in default
    # subject is what's pre-filled — not a crash and not the Dutch default.
    assert 'value="Your tickets for {{event_name}}"' in response.text
    assert 'value="Je tickets voor {{event_name}}"' not in response.text


async def test_save_with_unknown_language_falls_back_to_en_and_saves_under_en(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event(name="Fallback Save Event")
    token = await _get_editor_csrf_token(client, str(event.id))

    response = await client.post(
        f"/events/{event.id}/email-templates",
        data={
            "language": "fr",
            "subject": "Saved via fallback",
            "body": "<p>x</p>",
            "csrf_token": token,
        },
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/events/{event.id}/email-templates?language=en&flash=")

    saved_en = await client.get(f"/api/v1/events/{event.id}/email-templates/order_confirmation_ticket/en")
    assert saved_en.status_code == 200
    assert saved_en.json()["subject"] == "Saved via fallback"

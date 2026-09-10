"""Integration tests for the server-rendered backoffice web surface
(``app/web/routes/*``), exercised over real HTTP via the ASGI test client —
this is new attack surface (CSRF, session-cookie auth translated into HTML
redirects, an open-redirect guard, real HTML forms) added on top of the
JSON API, so it's tested directly rather than only unit-testing its helpers.

Covers:
- CSRF (``app/web/csrf.py``): missing/wrong token rejected, valid
  cookie+field pair accepted.
- ``WebAuthRequired``/``require_web_admin`` (``app/web/deps.py``):
  unauthenticated access to any backoffice page redirects to
  ``/login?next=<path>``.
- The open-redirect guard in ``app/web/routes/auth.py``'s ``_safe_next``,
  exercised end-to-end through the real login POST.
- Login flow: correct credentials set a session cookie and redirect; wrong
  credentials re-render the form with an error and set no session cookie.
- Theme editor page: the custom-CSS warning banner's presence/absence, and
  the contrast report table matching the JSON API's own numbers.
- The ``theme/preview-fragment`` HTMX endpoint: sanitized CSS + draft-only
  ``is_custom_css_active``, independent of what's actually saved.
- Image upload/delete and copy-from forms, happy path.
"""

from collections.abc import Awaitable, Callable
from urllib.parse import quote

from httpx import AsyncClient

from app.core.security import ADMIN_SESSION_COOKIE_NAME
from app.models.enums import AdminRole
from app.models.event import Event
from app.services.contrast import check_theme_contrast
from app.web.csrf import CSRF_COOKIE_NAME
from tests.integration.conftest import SeededAdmin

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_VALID_PNG_BYTES = _PNG_MAGIC + b"restofpngdata"


async def _api_login(client: AsyncClient, seeded: SeededAdmin) -> None:
    """Log in via the JSON API directly (bypassing the web login form/CSRF)
    for tests whose focus is something other than the login flow itself."""
    response = await client.post("/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password})
    assert response.status_code == 200


async def _create_event(client: AsyncClient, slug: str = "web-test-event") -> str:
    response = await client.post("/api/v1/events", json={"name": "Web Test Event", "slug": slug})
    assert response.status_code == 201
    return str(response.json()["id"])


async def _get_csrf_token(client: AsyncClient) -> str:
    """Visit GET /login to obtain a CSRF cookie, returning its value."""
    response = await client.get("/login")
    assert response.status_code == 200
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert token
    return token


# --- WebAuthRequired: unauthenticated redirect-to-login ---


async def test_unauthenticated_events_list_redirects_to_login_with_next(client: AsyncClient) -> None:
    response = await client.get("/events")

    assert response.status_code == 303
    assert response.headers["location"] == f"/login?next={quote('/events')}"


async def test_unauthenticated_theme_editor_redirects_to_login_with_the_original_path(
    client: AsyncClient, make_event: Callable[..., Awaitable[Event]]
) -> None:
    event = await make_event()
    path = f"/events/{event.id}/theme"

    response = await client.get(path)

    assert response.status_code == 303
    assert response.headers["location"] == f"/login?next={quote(path)}"


async def test_unauthenticated_orders_list_redirects_to_login_with_the_original_path(
    client: AsyncClient, make_event: Callable[..., Awaitable[Event]]
) -> None:
    """Milestone 4: mirrors the Theme editor's own redirect-with-``next``
    test above. The rest of this page's/its resend action's coverage
    (CSRF, happy path, 404/409 mapping, the ``order.total`` float-coercion
    regression) lives in ``tests/integration/test_web_orders_routes.py``.
    """
    event = await make_event()
    path = f"/events/{event.id}/orders"

    response = await client.get(path)

    assert response.status_code == 303
    assert response.headers["location"] == f"/login?next={quote(path)}"


async def test_unauthenticated_email_template_editor_redirects_to_login_with_the_original_path(
    client: AsyncClient, make_event: Callable[..., Awaitable[Event]]
) -> None:
    """Milestone 4: mirrors the Theme editor's own redirect-with-``next``
    test above. The rest of this page's coverage (pre-fill vs default,
    save/reset/preview-fragment, CSRF, language fallback) lives in
    ``tests/integration/test_web_email_templates_routes.py``.
    """
    event = await make_event()
    path = f"/events/{event.id}/email-templates"

    response = await client.get(path)

    assert response.status_code == 303
    assert response.headers["location"] == f"/login?next={quote(path)}"


async def test_root_no_longer_redirects_straight_into_the_backoffice(client: AsyncClient) -> None:
    """Post-launch fix (``app.web.routes.homepage``): ``/`` used to
    unconditionally redirect into the backoffice (``/events``), bouncing
    every unauthenticated buyer straight to ``/login``. It's now a real
    public homepage instead — see ``tests/integration/test_homepage_web_
    routes.py`` for the redirect-to-default-event/directory-listing
    behavior this asserts only that the OLD behavior is gone."""
    response = await client.get("/")
    assert response.status_code == 200
    assert response.headers.get("location") != "/events"


# --- Open-redirect guard, exercised end-to-end through the real login POST ---


async def test_login_with_absolute_url_next_falls_back_to_events(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    seeded = await make_admin_user()
    token = await _get_csrf_token(client)

    response = await client.post(
        "/login",
        data={
            "email": seeded.user.email,
            "password": seeded.password,
            "csrf_token": token,
            "next": "https://evil.example.com",
        },
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/events"


async def test_login_with_protocol_relative_next_falls_back_to_events(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    seeded = await make_admin_user()
    token = await _get_csrf_token(client)

    response = await client.post(
        "/login",
        data={"email": seeded.user.email, "password": seeded.password, "csrf_token": token, "next": "//evil.example.com"},
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/events"


async def test_login_with_a_safe_relative_next_is_honored(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    seeded = await make_admin_user()
    event = await make_event()
    token = await _get_csrf_token(client)
    safe_path = f"/events/{event.id}/theme"

    response = await client.post(
        "/login", data={"email": seeded.user.email, "password": seeded.password, "csrf_token": token, "next": safe_path}
    )

    assert response.status_code == 303
    assert response.headers["location"] == safe_path


# --- CSRF ---


async def test_login_post_with_missing_csrf_field_is_rejected(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    seeded = await make_admin_user()

    response = await client.post("/login", data={"email": seeded.user.email, "password": seeded.password})

    assert response.status_code == 422  # required Form(...) field entirely absent
    assert ADMIN_SESSION_COOKIE_NAME not in response.cookies


async def test_login_post_with_wrong_csrf_token_is_rejected_403(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    seeded = await make_admin_user()
    await _get_csrf_token(client)  # sets a real cookie...

    response = await client.post(
        "/login",
        data={
            "email": seeded.user.email,
            "password": seeded.password,
            "csrf_token": "not-the-real-token",  # ...but we submit a different value
        },
    )

    assert response.status_code == 403
    assert ADMIN_SESSION_COOKIE_NAME not in response.cookies


async def test_login_post_with_no_cookie_at_all_is_rejected_403(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    """No prior GET /login means no CSRF cookie was ever set — any submitted
    token must fail to match a nonexistent cookie."""
    seeded = await make_admin_user()

    response = await client.post(
        "/login", data={"email": seeded.user.email, "password": seeded.password, "csrf_token": "anything-at-all"}
    )

    assert response.status_code == 403


async def test_theme_save_form_with_missing_csrf_is_rejected(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()

    response = await client.post(
        f"/events/{event.id}/theme",
        data={
            "primary_color": "#111111",
            "secondary_color": "#eeeeee",
            "accent_color": "#c9a227",
            "font_choice": "system-sans",
            "status": "draft",
        },
    )

    assert response.status_code == 422


async def test_theme_save_form_with_wrong_csrf_is_rejected_403(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()

    response = await client.post(
        f"/events/{event.id}/theme",
        data={
            "primary_color": "#111111",
            "secondary_color": "#eeeeee",
            "accent_color": "#c9a227",
            "font_choice": "system-sans",
            "status": "draft",
            "csrf_token": "wrong-token",
        },
    )

    assert response.status_code == 403


async def test_theme_save_form_with_valid_matching_csrf_succeeds(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()
    # A page render both sets the CSRF cookie and embeds a matching token.
    editor_page = await client.get(f"/events/{event.id}/theme")
    assert editor_page.status_code == 200
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert token

    response = await client.post(
        f"/events/{event.id}/theme",
        data={
            "primary_color": "#111111",
            "secondary_color": "#eeeeee",
            "accent_color": "#c9a227",
            "font_choice": "system-sans",
            "status": "draft",
            "csrf_token": token,
        },
    )

    assert response.status_code == 303
    assert "Theme%20saved" in response.headers["location"]


# --- Login flow: success/failure ---


async def test_login_with_correct_credentials_sets_cookie_and_redirects(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    seeded = await make_admin_user()
    token = await _get_csrf_token(client)

    response = await client.post(
        "/login", data={"email": seeded.user.email, "password": seeded.password, "csrf_token": token}
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/events"
    assert ADMIN_SESSION_COOKIE_NAME in response.cookies


async def test_login_with_wrong_password_rerenders_form_with_error_and_sets_no_session_cookie(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    seeded = await make_admin_user()
    token = await _get_csrf_token(client)

    response = await client.post(
        "/login", data={"email": seeded.user.email, "password": "definitely-wrong-password", "csrf_token": token}
    )

    assert response.status_code == 401
    assert "Invalid email or password" in response.text
    assert ADMIN_SESSION_COOKIE_NAME not in response.cookies


async def test_already_authenticated_visitor_is_bounced_off_the_login_form(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())

    response = await client.get("/login")

    assert response.status_code == 303
    assert response.headers["location"] == "/events"


# --- Scanner-role login default redirect (Milestone 7) ---
#
# app.web.routes.auth._apply_role_default: a scanner-role session landing on
# this module's own "/events" default (i.e. no explicit ?next= was given)
# is retargeted to the show-picker ("/scan") instead, since "/events" is
# unreachable for that role (require_web_admin excludes scanner-role — see
# app/web/deps.py). An admin-role login must see no change in behavior, and
# an explicit ?next= for either role must still be honored exactly as
# before (the existing _safe_next guard, untouched by this change).


async def test_scanner_login_with_no_explicit_next_lands_on_scan_not_events(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    seeded = await make_admin_user(role=AdminRole.SCANNER)
    token = await _get_csrf_token(client)

    response = await client.post(
        "/login", data={"email": seeded.user.email, "password": seeded.password, "csrf_token": token}
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/scan"


async def test_admin_login_with_no_explicit_next_still_lands_on_events(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    """No-regression check: the scanner-only retarget must not change the
    pre-existing admin-role default."""
    seeded = await make_admin_user(role=AdminRole.ADMIN)
    token = await _get_csrf_token(client)

    response = await client.post(
        "/login", data={"email": seeded.user.email, "password": seeded.password, "csrf_token": token}
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/events"


async def test_scanner_login_with_explicit_next_is_still_honored(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    """A scanner-role login with an explicit, safe ?next= is NOT overridden
    by the /scan retarget — that only applies when no next was requested
    (see _apply_role_default's docstring: "/events" is reused as the "no
    explicit next" sentinel, so an *explicit* non-"/events" next always
    passes through untouched regardless of role)."""
    seeded = await make_admin_user(role=AdminRole.SCANNER)
    event = await make_event()
    token = await _get_csrf_token(client)
    safe_path = f"/events/{event.id}/theme"

    response = await client.post(
        "/login", data={"email": seeded.user.email, "password": seeded.password, "csrf_token": token, "next": safe_path}
    )

    assert response.status_code == 303
    assert response.headers["location"] == safe_path


async def test_admin_login_with_explicit_next_is_still_honored(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    seeded = await make_admin_user(role=AdminRole.ADMIN)
    token = await _get_csrf_token(client)

    response = await client.post(
        "/login", data={"email": seeded.user.email, "password": seeded.password, "csrf_token": token, "next": "/scan"}
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/scan"


# --- Theme editor page: warning banner ---


async def test_theme_editor_shows_no_banner_when_no_theme_saved_yet(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()

    response = await client.get(f"/events/{event.id}/theme")

    assert response.status_code == 200
    assert "Custom CSS is active" not in response.text


async def test_theme_editor_shows_no_banner_when_saved_theme_has_no_custom_css(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())
    event_id = await _create_event(client, slug="banner-none-event")
    await client.put(
        f"/api/v1/events/{event_id}/theme",
        json={"primary_color": "#111111", "secondary_color": "#eeeeee", "accent_color": "#c9a227"},
    )

    response = await client.get(f"/events/{event_id}/theme")

    assert response.status_code == 200
    assert "Custom CSS is active" not in response.text


async def test_theme_editor_shows_banner_when_saved_theme_has_active_custom_css(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())
    event_id = await _create_event(client, slug="banner-active-event")
    await client.put(
        f"/api/v1/events/{event_id}/theme",
        json={
            "primary_color": "#111111",
            "secondary_color": "#eeeeee",
            "accent_color": "#c9a227",
            "custom_css": ".event-content { color: blue; }",
        },
    )

    response = await client.get(f"/events/{event_id}/theme")

    assert response.status_code == 200
    assert "Custom CSS is active" in response.text


# --- Contrast report table matches the JSON API's own numbers ---


async def test_theme_editor_contrast_table_matches_json_api_pass_fail_counts(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())
    event_id = await _create_event(client, slug="contrast-table-event")
    # All-white colors: several pairs will fail even the large-text threshold.
    await client.put(
        f"/api/v1/events/{event_id}/theme",
        json={"primary_color": "#ffffff", "secondary_color": "#ffffff", "accent_color": "#fefefe"},
    )
    api_report = (await client.get(f"/api/v1/events/{event_id}/theme")).json()["contrast_report"]
    expected_pass = sum(p["passes_normal_text"] for p in api_report["pairs"]) + sum(
        p["passes_large_text"] for p in api_report["pairs"]
    )
    expected_fail = (len(api_report["pairs"]) * 2) - expected_pass

    response = await client.get(f"/events/{event_id}/theme")

    assert response.status_code == 200
    assert response.text.count("bo-badge--pass") == expected_pass
    assert response.text.count("bo-badge--fail") == expected_fail


async def test_check_theme_contrast_sanity_all_white_has_failures() -> None:
    """Guards the fixture assumption above: all-white colors must actually
    produce at least one failing pair, or the test above would trivially
    pass with expected_fail == 0 and prove nothing."""
    report = check_theme_contrast(primary_color="#ffffff", secondary_color="#ffffff", accent_color="#fefefe")
    assert not report.all_pass_normal_text


# --- theme/preview-fragment HTMX endpoint ---


async def test_preview_fragment_reflects_draft_sanitized_css_independent_of_saved_theme(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())
    event_id = await _create_event(client, slug="fragment-draft-event")
    await client.put(f"/api/v1/events/{event_id}/theme", json={
        "primary_color": "#111111", "secondary_color": "#eeeeee", "accent_color": "#c9a227",
    })  # saved theme has no custom CSS
    editor_page = await client.get(f"/events/{event_id}/theme")
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert editor_page.status_code == 200 and token

    response = await client.post(
        f"/events/{event_id}/theme/preview-fragment",
        data={
            "primary_color": "#111111",
            "secondary_color": "#eeeeee",
            "accent_color": "#c9a227",
            "font_choice": "system-sans",
            "custom_css": "body { color: red; } .event-content { color: teal; }",
            "csrf_token": token,
        },
    )

    assert response.status_code == 200
    assert "color: teal" in response.text
    assert "color: red" not in response.text  # the unscoped `body` rule was stripped
    assert "This draft's custom CSS is included above (not yet saved)." in response.text

    # The saved theme itself must be unaffected by this preview-only call.
    saved = await client.get(f"/api/v1/events/{event_id}/theme")
    assert saved.json()["is_custom_css_active"] is False


async def test_preview_fragment_with_no_custom_css_shows_the_none_set_message(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()
    editor_page = await client.get(f"/events/{event.id}/theme")
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert editor_page.status_code == 200 and token

    response = await client.post(
        f"/events/{event.id}/theme/preview-fragment",
        data={
            "primary_color": "#111111",
            "secondary_color": "#eeeeee",
            "accent_color": "#c9a227",
            "font_choice": "system-sans",
            "custom_css": "",
            "csrf_token": token,
        },
    )

    assert response.status_code == 200
    assert "No custom CSS is currently set" in response.text
    assert "This draft's custom CSS is included above" not in response.text


async def test_preview_fragment_unauthenticated_redirects_to_login(
    client: AsyncClient, make_event: Callable[..., Awaitable[Event]]
) -> None:
    event = await make_event()

    response = await client.post(
        f"/events/{event.id}/theme/preview-fragment",
        data={
            "primary_color": "#111111",
            "secondary_color": "#eeeeee",
            "accent_color": "#c9a227",
            "font_choice": "system-sans",
            "custom_css": "",
            "csrf_token": "irrelevant",
        },
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


# --- Image upload/delete + copy-from forms: happy path ---


async def test_logo_upload_and_delete_forms_happy_path(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()
    editor_page = await client.get(f"/events/{event.id}/theme")
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert editor_page.status_code == 200 and token

    upload_response = await client.post(
        f"/events/{event.id}/theme/logo",
        data={"csrf_token": token},
        files={"file": ("logo.png", _VALID_PNG_BYTES, "image/png")},
    )
    assert upload_response.status_code == 303
    assert "Logo%20uploaded" in upload_response.headers["location"]

    after_upload_page = await client.get(f"/events/{event.id}/theme")
    assert "/uploads/themes/" in after_upload_page.text

    delete_response = await client.post(f"/events/{event.id}/theme/logo/delete", data={"csrf_token": token})
    assert delete_response.status_code == 303

    after_delete_page = await client.get(f"/events/{event.id}/theme")
    assert "No logo uploaded yet." in after_delete_page.text


async def test_background_upload_form_happy_path(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()
    editor_page = await client.get(f"/events/{event.id}/theme")
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert editor_page.status_code == 200 and token

    response = await client.post(
        f"/events/{event.id}/theme/background",
        data={"csrf_token": token},
        files={"file": ("bg.png", _VALID_PNG_BYTES, "image/png")},
    )

    assert response.status_code == 303
    after = await client.get(f"/events/{event.id}/theme")
    assert "/uploads/themes/" in after.text


async def test_copy_from_form_happy_path(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())
    source_event_id = await _create_event(client, slug="web-copy-source")
    target_event_id = await _create_event(client, slug="web-copy-target")
    await client.put(
        f"/api/v1/events/{source_event_id}/theme",
        json={"primary_color": "#222222", "secondary_color": "#dddddd", "accent_color": "#0000aa"},
    )
    editor_page = await client.get(f"/events/{target_event_id}/theme")
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert editor_page.status_code == 200 and token
    assert source_event_id in editor_page.text  # listed as an option to copy from

    response = await client.post(
        f"/events/{target_event_id}/theme/copy-from", data={"source_event_id": source_event_id, "csrf_token": token}
    )

    assert response.status_code == 303
    saved = await client.get(f"/api/v1/events/{target_event_id}/theme")
    assert saved.json()["primary_color"] == "#222222"


# --- Events list: copy-preview-link button (Milestone 2) ------------------


async def test_events_list_renders_copy_preview_link_button_with_correct_url(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())
    event_id = await _create_event(client, slug="preview-link-test")
    event = (await client.get(f"/api/v1/events/{event_id}")).json()

    response = await client.get("/events")

    assert response.status_code == 200
    assert "Copy preview link" in response.text
    assert f'data-copy-value="http://localhost:8000/preview/{event["preview_token"]}"' in response.text

"""Unit tests for ``app.web.public_context``: pure helpers behind the
public-site route module (``app.web.routes.public_site``) — locale
resolution, per-Theme CSS assembly, ``schema.org/Event`` JSON-LD, and
social share URLs. No DB/HTTP machinery involved (see that module's own
docstring); a minimal hand-built ``starlette.requests.Request`` is enough
for the functions that need one.
"""

import json

from starlette.requests import Request

from app.models.enums import ThemeFont
from app.web.public_context import (
    LOCALE_COOKIE_NAME,
    build_event_json_ld,
    build_public_theme_css,
    build_share_urls,
    resolve_locale,
)


def _make_request(*, query_string: str = "", headers: dict[str, str] | None = None) -> Request:
    """A bare-minimum ASGI ``http`` scope, enough for ``Request.query_params``,
    ``Request.headers``, and ``Request.cookies`` to work correctly."""
    raw_headers = [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": query_string.encode(),
        "headers": raw_headers,
    }
    return Request(scope)


# --- resolve_locale: query param > cookie > Accept-Language > default -----


def test_resolve_locale_defaults_to_en_with_no_signal_at_all() -> None:
    request = _make_request()
    assert resolve_locale(request) == "en"


def test_resolve_locale_uses_explicit_query_param_override() -> None:
    request = _make_request(query_string="lang=nl")
    assert resolve_locale(request) == "nl"


def test_resolve_locale_ignores_unsupported_query_param() -> None:
    request = _make_request(query_string="lang=fr", headers={"accept-language": "nl"})
    # "fr" isn't supported, so precedence falls through to the next signal.
    assert resolve_locale(request) == "nl"


def test_resolve_locale_falls_back_to_cookie_when_no_query_param() -> None:
    request = _make_request(headers={"cookie": f"{LOCALE_COOKIE_NAME}=nl"})
    assert resolve_locale(request) == "nl"


def test_resolve_locale_query_param_beats_cookie() -> None:
    request = _make_request(
        query_string="lang=en", headers={"cookie": f"{LOCALE_COOKIE_NAME}=nl"}
    )
    assert resolve_locale(request) == "en"


def test_resolve_locale_cookie_beats_accept_language() -> None:
    request = _make_request(
        headers={"cookie": f"{LOCALE_COOKIE_NAME}=en", "accept-language": "nl-NL,nl;q=0.9"}
    )
    assert resolve_locale(request) == "en"


def test_resolve_locale_parses_accept_language_with_region_and_quality() -> None:
    request = _make_request(headers={"accept-language": "nl-NL,nl;q=0.9,en;q=0.8"})
    assert resolve_locale(request) == "nl"


def test_resolve_locale_falls_back_to_default_for_unsupported_accept_language() -> None:
    request = _make_request(headers={"accept-language": "de-DE,de;q=0.9"})
    assert resolve_locale(request) == "en"


# --- build_public_theme_css -------------------------------------------


def _theme(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "primary_color": "#111111",
        "secondary_color": "#eeeeee",
        "accent_color": "#c9a227",
        "font_choice": ThemeFont.SYSTEM_SANS,
        "logo_url": None,
        "background_image_url": None,
        "custom_css": None,
    }
    base.update(overrides)
    return base


def test_build_public_theme_css_returns_empty_string_for_no_theme() -> None:
    assert build_public_theme_css(None) == ""


def test_build_public_theme_css_includes_fixed_color_fields() -> None:
    css = build_public_theme_css(_theme())
    assert "--beacon-color-primary: #111111;" in css
    assert "--beacon-color-secondary: #eeeeee;" in css
    assert "--beacon-color-accent: #c9a227;" in css
    assert ".event-content {" in css


def test_build_public_theme_css_includes_hero_rule_only_when_background_image_set() -> None:
    without_bg = build_public_theme_css(_theme())
    assert ".pub-hero {" not in without_bg

    with_bg = build_public_theme_css(_theme(background_image_url="/uploads/bg.jpg"))
    assert ".pub-hero {" in with_bg
    assert "url('/uploads/bg.jpg')" in with_bg


def test_build_public_theme_css_appends_custom_css_verbatim() -> None:
    css = build_public_theme_css(_theme(custom_css=".event-content h1 { color: red; }"))
    assert css.rstrip().endswith(".event-content h1 { color: red; }")


def test_build_public_theme_css_omits_custom_css_block_when_none_set() -> None:
    css = build_public_theme_css(_theme(custom_css=None))
    assert css.count(".event-content {") == 1  # only the base rule, nothing appended


# --- build_event_json_ld -------------------------------------------------


def _event(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": "Christmas Passion",
        "description": None,
        "shows": [
            {
                "date": "2026-12-18",
                "start_time": "20:00:00",
                "venue_name": "Het Kruispunt",
                "venue_address": "Kerkstraat 1, Landsmeer",
                "ticket_types": [
                    {"id": "tt-1", "name": "Adult", "price": "15.00", "remaining": 10},
                    {"id": "tt-2", "name": "Child", "price": "7.50", "remaining": 0},
                ],
            }
        ],
    }
    base.update(overrides)
    return base


def test_build_event_json_ld_is_well_formed_and_parseable() -> None:
    raw = build_event_json_ld(_event(), "https://example.test/e/christmas-passion")
    entries = json.loads(raw)
    assert isinstance(entries, list)
    assert len(entries) == 1


def test_build_event_json_ld_has_required_schema_org_fields() -> None:
    raw = build_event_json_ld(_event(), "https://example.test/e/christmas-passion")
    entry = json.loads(raw)[0]
    assert entry["@context"] == "https://schema.org"
    assert entry["@type"] == "Event"
    assert entry["name"] == "Christmas Passion"
    assert entry["startDate"] == "2026-12-18T20:00:00"
    assert entry["location"]["@type"] == "Place"
    assert entry["location"]["name"] == "Het Kruispunt"
    assert entry["location"]["address"] == "Kerkstraat 1, Landsmeer"
    assert len(entry["offers"]) == 2


def test_build_event_json_ld_availability_reflects_remaining_stock() -> None:
    raw = build_event_json_ld(_event(), "https://example.test/e/christmas-passion")
    offers = json.loads(raw)[0]["offers"]
    in_stock = next(o for o in offers if o["name"] == "Adult")
    sold_out = next(o for o in offers if o["name"] == "Child")
    assert in_stock["availability"] == "https://schema.org/InStock"
    assert sold_out["availability"] == "https://schema.org/SoldOut"


def test_build_event_json_ld_includes_description_only_when_present() -> None:
    without_description = json.loads(build_event_json_ld(_event(), "https://example.test/x"))[0]
    assert "description" not in without_description

    with_description = json.loads(
        build_event_json_ld(_event(description="A festive concert."), "https://example.test/x")
    )[0]
    assert with_description["description"] == "A festive concert."


def test_build_event_json_ld_one_entry_per_show() -> None:
    event = _event(
        shows=[
            {
                "date": "2026-12-18",
                "start_time": "20:00:00",
                "venue_name": "Venue A",
                "venue_address": "Address A",
                "ticket_types": [],
            },
            {
                "date": "2026-12-19",
                "start_time": "20:00:00",
                "venue_name": "Venue B",
                "venue_address": "Address B",
                "ticket_types": [],
            },
        ]
    )
    entries = json.loads(build_event_json_ld(event, "https://example.test/x"))
    assert len(entries) == 2
    assert {e["location"]["name"] for e in entries} == {"Venue A", "Venue B"}


def test_build_event_json_ld_escapes_closing_script_tag_in_event_name() -> None:
    """A malicious/careless event name containing a literal ``</script>``
    must not be able to prematurely close the ``<script>`` block it's
    embedded in (see the module's ``_escape_for_script_embedding`` docstring)."""
    event = _event(name="Evil</script><script>alert(1)</script>")
    raw = build_event_json_ld(event, "https://example.test/x")
    assert "</script>" not in raw
    # The escaped payload still round-trips to the exact original string
    # once un-escaped, i.e. no data was lost/corrupted, only made script-safe.
    entries = json.loads(raw.replace("\\u003c", "<"))
    assert entries[0]["name"] == "Evil</script><script>alert(1)</script>"


# --- build_share_urls ------------------------------------------------


def test_build_share_urls_contains_all_three_expected_channels() -> None:
    urls = build_share_urls(page_url="https://example.test/e/x", share_text="Tickets for X")
    assert set(urls.keys()) == {"whatsapp", "facebook", "email"}


def test_build_share_urls_whatsapp_includes_encoded_text_and_url() -> None:
    urls = build_share_urls(page_url="https://example.test/e/x", share_text="Tickets for X")
    assert urls["whatsapp"].startswith("https://wa.me/?text=")
    assert "Tickets%20for%20X" in urls["whatsapp"]
    assert "example.test" in urls["whatsapp"]


def test_build_share_urls_facebook_targets_the_sharer_endpoint_with_encoded_url() -> None:
    # `urllib.parse.quote`'s default `safe="/"` leaves forward slashes
    # un-encoded, so only `:`, `?`, `=` etc. get percent-escaped here.
    urls = build_share_urls(page_url="https://example.test/e/x?y=1", share_text="Tickets for X")
    assert urls["facebook"] == (
        "https://www.facebook.com/sharer/sharer.php?u=https%3A//example.test/e/x%3Fy%3D1"
    )


def test_build_share_urls_email_is_a_mailto_link_with_subject_and_body() -> None:
    urls = build_share_urls(page_url="https://example.test/e/x", share_text="Tickets for X")
    assert urls["email"].startswith("mailto:?subject=Tickets%20for%20X&body=")
    assert "example.test" in urls["email"]

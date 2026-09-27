"""Pure builders for public-page context: locale, theme CSS, JSON-LD and share
links. Kept out of the routes so they're easy to test.
"""

import json
from typing import Any
from urllib.parse import quote

from fastapi import Request

from app.core.css_sanitizer import EVENT_CONTENT_CLASS
from app.i18n import DEFAULT_LOCALE, SUPPORTED_LOCALES
from app.models.enums import ThemeFont
from app.services.color import derive_dark_palette
from app.services.theme_preview import FONT_STACKS

LOCALE_COOKIE_NAME = "beacon_locale"


def resolve_locale(request: Request) -> str:
    """``?lang=`` > locale cookie > ``Accept-Language`` > default."""
    query_lang = request.query_params.get("lang")
    if query_lang in SUPPORTED_LOCALES:
        return query_lang

    cookie_lang = request.cookies.get(LOCALE_COOKIE_NAME)
    if cookie_lang in SUPPORTED_LOCALES:
        return cookie_lang

    accept_language = request.headers.get("accept-language", "")
    for part in accept_language.split(","):
        code = part.split(";")[0].strip().split("-")[0].lower()
        if code in SUPPORTED_LOCALES:
            return code

    return DEFAULT_LOCALE


def build_public_theme_css(theme: dict[str, Any] | None) -> str:
    """The ``<style>`` body for an event's public pages: custom properties for the
    fixed colors and font (same as the backoffice preview), the hero image,
    a computed dark-mode palette, then the sanitized custom CSS last (so it
    can override everything).

    Without a theme there's no theme CSS at all — never a baked-in default
    palette. Site chrome has its own separate dark palette in ``public.css``.
    """
    if theme is None:
        return ""

    font_stack = FONT_STACKS[ThemeFont(theme["font_choice"])]
    base_css = (
        f".{EVENT_CONTENT_CLASS} {{\n"
        f"  --beacon-color-primary: {theme['primary_color']};\n"
        f"  --beacon-color-secondary: {theme['secondary_color']};\n"
        f"  --beacon-color-accent: {theme['accent_color']};\n"
        f"  font-family: {font_stack};\n"
        f"  color: var(--beacon-color-primary);\n"
        f"  background-color: var(--beacon-color-secondary);\n"
        f"}}\n"
        # Only the primary button, not every .pub-button: ghost/small buttons
        # (share, copy link) must keep their outline look.
        f".{EVENT_CONTENT_CLASS} .pub-button--primary {{\n"
        f"  background: var(--beacon-color-accent);\n"
        f"  color: var(--beacon-color-secondary);\n"
        f"  border-color: var(--beacon-color-accent);\n"
        f"}}\n"
    )
    if theme.get("background_image_url"):
        # A dark scrim keeps hero text legible over arbitrary photos (it can't
        # guarantee AA; automated checks only cover the fixed colors).
        base_css += (
            f".{EVENT_CONTENT_CLASS} .pub-hero {{\n"
            f"  background-image: linear-gradient(rgba(0,0,0,.45), rgba(0,0,0,.45)),"
            f" url('{theme['background_image_url']}');\n"
            f"  background-size: cover;\n"
            f"  background-position: center;\n"
            f"  color: #ffffff;\n"
            f"}}\n"
        )

    dark = derive_dark_palette(
        primary_color=theme["primary_color"],
        secondary_color=theme["secondary_color"],
        accent_color=theme["accent_color"],
    )
    base_css += (
        f"@media (prefers-color-scheme: dark) {{\n"
        f"  .{EVENT_CONTENT_CLASS} {{\n"
        f"    --beacon-color-primary: {dark['text']};\n"
        f"    --beacon-color-secondary: {dark['background']};\n"
        f"    --beacon-color-accent: {dark['accent']};\n"
        f"  }}\n"
        f"}}\n"
    )

    custom_css = theme.get("custom_css") or ""
    # Already sanitized on save; appended last so it can override the dark
    # palette too.
    return base_css if not custom_css else f"{base_css}\n{custom_css}\n"


def build_beamer_theme_css(theme: dict[str, Any] | None) -> str:
    """Minimal theme CSS for the beamer/TV view: font and accent only.

    The screen is read from across a room with nobody to intervene, so
    ``beamer.css`` uses its own high-contrast text and background, and the
    accent is used only for a decorative bar. Custom CSS is never applied here.
    """
    if theme is None:
        return ""

    font_stack = FONT_STACKS[ThemeFont(theme["font_choice"])]
    return (
        ".beamer-page {\n"
        f"  --beacon-color-accent: {theme['accent_color']};\n"
        f"  font-family: {font_stack};\n"
        "}\n"
    )


def _escape_for_script_embedding(json_text: str) -> str:
    """Escape ``<`` so a value containing ``</script`` can't end the JSON-LD
    ``<script>`` block early.
    """
    return json_text.replace("<", "\\u003c")


def build_event_json_ld(event: dict[str, Any], page_url: str) -> str:
    """``schema.org/Event`` JSON-LD, one object per show, with availability from
    live stock. Currency is EUR (there's no currency field yet).
    """
    entries = []
    for show in event["shows"]:
        offers = [
            {
                "@type": "Offer",
                "name": ticket_type["name"],
                "price": str(ticket_type["price"]),
                "priceCurrency": "EUR",
                "availability": (
                    "https://schema.org/InStock" if ticket_type["remaining"] > 0 else "https://schema.org/SoldOut"
                ),
                "url": f"{page_url}?ticket_type={ticket_type['id']}",
            }
            for ticket_type in show["ticket_types"]
        ]
        entry: dict[str, Any] = {
            "@context": "https://schema.org",
            "@type": "Event",
            "name": event["name"],
            "startDate": f"{show['date']}T{show['start_time']}",
            "eventStatus": "https://schema.org/EventScheduled",
            "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
            "location": {
                "@type": "Place",
                "name": show["venue_name"],
                "address": show["venue_address"],
            },
            "offers": offers,
        }
        if event.get("description"):
            entry["description"] = event["description"]
        entries.append(entry)
    return _escape_for_script_embedding(json.dumps(entries))


def build_share_urls(*, page_url: str, share_text: str) -> dict[str, str]:
    """WhatsApp/Facebook/email share links. Facebook ignores text and reads the
    page's Open Graph tags instead.
    """
    return {
        "whatsapp": f"https://wa.me/?text={quote(f'{share_text} {page_url}')}",
        "facebook": f"https://www.facebook.com/sharer/sharer.php?u={quote(page_url)}",
        "email": f"mailto:?subject={quote(share_text)}&body={quote(page_url)}",
    }

"""Pure helper functions building the render context for public-site pages
(``app.web.routes.public_site``): locale resolution, per-Theme CSS,
``schema.org/Event`` JSON-LD, and social share URLs.

Kept separate from the route module so the route functions stay thin
(fetch data, call these builders, render a template) and so these pieces
can be reasoned about (and eventually unit-tested by `test-writer`) without
any FastAPI request/response machinery involved.
"""

import json
from typing import Any
from urllib.parse import quote

from fastapi import Request

from app.core.css_sanitizer import EVENT_CONTENT_CLASS
from app.i18n import DEFAULT_LOCALE, SUPPORTED_LOCALES
from app.models.enums import ThemeFont
from app.services.theme_preview import FONT_STACKS

LOCALE_COOKIE_NAME = "beacon_locale"


def resolve_locale(request: Request) -> str:
    """Resolve the buyer's display locale, per PROJECT_BRIEF.md's
    Internationalization section: "Buyer selects language on the public
    site or it's inferred from browser locale with a visible override".

    Precedence: an explicit ``?lang=`` query param (the visible override,
    e.g. a language-switcher link) > a previously-set locale cookie (so the
    override persists across navigation) > the browser's ``Accept-Language``
    header > :data:`app.i18n.DEFAULT_LOCALE`.
    """
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
    """Build the ``<style>`` body for one Event's public pages: CSS custom
    properties + font-family for the Theme's fixed fields (mirrors
    ``app.services.theme_preview.build_theme_preview``'s base-CSS shape, so
    the backoffice live-preview pane and the real public page render the
    fixed fields identically), a hero background-image rule if set, and the
    Theme's already-sanitized ``custom_css`` appended verbatim.

    Never invents a default palette of its own if ``theme`` is ``None`` —
    per PROJECT_BRIEF.md's Event & Theming section, every themeable surface
    must pull its colors from the active Event's Theme, never a hardcoded
    default that can't be overridden; an Event with no Theme row yet simply
    renders with no theme CSS at all (plain, unstyled content), not a
    silently-baked-in poster palette.
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
        f".{EVENT_CONTENT_CLASS} .pub-button {{\n"
        f"  background: var(--beacon-color-accent);\n"
        f"  color: var(--beacon-color-secondary);\n"
        f"  border-color: var(--beacon-color-accent);\n"
        f"}}\n"
    )
    if theme.get("background_image_url"):
        # A dark scrim under the uploaded image, not the raw image alone,
        # so arbitrary buyer-uploaded photography doesn't wash out the
        # hero text on top of it — mitigates but can't guarantee AA
        # contrast for an arbitrary image (flagged for accessibility-
        # auditor as a manual-check item; automated contrast checking only
        # covers the fixed color fields, see app.services.contrast).
        base_css += (
            f".{EVENT_CONTENT_CLASS} .pub-hero {{\n"
            f"  background-image: linear-gradient(rgba(0,0,0,.45), rgba(0,0,0,.45)),"
            f" url('{theme['background_image_url']}');\n"
            f"  background-size: cover;\n"
            f"  background-position: center;\n"
            f"  color: #ffffff;\n"
            f"}}\n"
        )

    custom_css = theme.get("custom_css") or ""
    # custom_css already went through app.core.css_sanitizer.sanitize_custom_css
    # at save time (see app.schemas.public.PublicThemeOut docstring) and is
    # guaranteed scoped to EVENT_CONTENT_CLASS and safe for <style> embedding
    # (angle brackets pre-escaped) — never re-sanitized or re-escaped here.
    return base_css if not custom_css else f"{base_css}\n{custom_css}\n"


def _escape_for_script_embedding(json_text: str) -> str:
    """Prevent a value inside the JSON payload (e.g. an event/venue name)
    from prematurely closing the ``<script>`` tag it's embedded in.
    ``json.dumps`` does not escape ``<`` by default; a literal ``</script``
    substring in any string value would otherwise end the script block
    early and let the remainder run as page HTML — same reasoning as
    ``app.core.css_sanitizer``'s angle-bracket escaping for ``<style>``
    embedding."""
    return json_text.replace("<", "\\u003c")


def build_event_json_ld(event: dict[str, Any], page_url: str) -> str:
    """Build one ``schema.org/Event`` JSON-LD object per Show under
    ``event`` (each performance date is its own bookable Event instance),
    serialized as a JSON array ready to embed in a single
    ``<script type="application/ld+json">`` block.

    Per PROJECT_BRIEF.md's SEO & Discoverability section: name, startDate,
    location, offers/pricing, and availability (derived from each
    TicketType's live ``remaining``) — the same fields also serve as the
    "machine-readable content for LLM-based search" the brief calls for,
    since it's the same structured-data discipline, not a separate format.

    Currency is hardcoded to EUR: no currency field exists anywhere in the
    data model yet (flagged in this milestone's handoff — every Event this
    platform currently supports is single-currency/EUR-only by omission,
    not by an explicit stated requirement).
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
    """Build WhatsApp/Facebook/email share links for the public share
    buttons (per PROJECT_BRIEF.md's Sharing section: "Social share buttons
    ... WhatsApp/Facebook/email at minimum"). ``share_text`` (already
    translated/composed by the caller) is included in the WhatsApp/email
    share content; Facebook's sharer ignores any text param and only reads
    Open Graph tags from ``page_url`` itself, which the page already sets.
    """
    return {
        "whatsapp": f"https://wa.me/?text={quote(f'{share_text} {page_url}')}",
        "facebook": f"https://www.facebook.com/sharer/sharer.php?u={quote(page_url)}",
        "email": f"mailto:?subject={quote(share_text)}&body={quote(page_url)}",
    }

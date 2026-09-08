"""Unit tests for ``app.i18n.translate`` and Dutch translation coverage.

Per ``PROJECT_BRIEF.md``'s Internationalization section, EN and NL must
both be "supported from the first milestone, not retrofitted later". This
doesn't assert exact NL wording for every key (that's a content/copy
concern, not a business-logic one) — it asserts the two things that would
actually break the guarantee silently: that ``translate(key, "nl")``
resolves to real Dutch text (not the English fallback, not the raw key),
and that NL translation coverage is *complete* relative to EN, so a future
English-only addition to ``en.json`` fails a test instead of quietly
falling back to English forever on the Dutch site.

Pure, no DB/app context needed.
"""

import json
from pathlib import Path

from app.i18n import translate

_LOCALES_DIR = Path(__file__).resolve().parent.parent.parent / "app" / "i18n"
_EN_STRINGS: dict[str, str] = json.loads((_LOCALES_DIR / "en.json").read_text(encoding="utf-8"))
_NL_STRINGS: dict[str, str] = json.loads((_LOCALES_DIR / "nl.json").read_text(encoding="utf-8"))


def test_every_english_key_has_a_dutch_counterpart() -> None:
    """The completeness guarantee: any key present in ``en.json`` must also
    be present in ``nl.json``, so a future untranslated addition fails here
    rather than silently falling back to English on the Dutch site."""
    missing = sorted(set(_EN_STRINGS) - set(_NL_STRINGS))
    assert missing == [], f"Keys missing from nl.json: {missing}"


def test_every_dutch_value_is_non_empty_and_not_a_placeholder() -> None:
    """Guards against a key existing in nl.json but with an empty string or
    an obvious placeholder (e.g. 'TODO') standing in for a real
    translation — which would pass a plain "key exists" check but still
    not be real Dutch content."""
    for key, value in _NL_STRINGS.items():
        assert value.strip() != "", f"nl.json[{key!r}] is empty"
        assert value.strip().upper() != "TODO", f"nl.json[{key!r}] is a placeholder"


_LEGITIMATELY_IDENTICAL_KEYS = frozenset(
    {
        "app.name",  # proper noun, not translated in either locale
        "public.landing.countdown_unit_days",  # "d" abbreviates "dagen" in nl too
        "public.landing.countdown_unit_minutes",  # "m" abbreviates "minuten" in nl too
        "public.landing.countdown_unit_seconds",  # "s" abbreviates "seconden" in nl too
        "public.order_confirmation.tickets_heading",  # "Tickets" is an established loanword in Dutch
    }
)


def test_every_dutch_value_differs_from_the_english_value_except_known_loanwords_and_abbreviations() -> None:
    """A NL string identical to its EN counterpart is almost always a sign
    the string was copy-pasted rather than translated. A short allowlist
    covers the genuine exceptions actually present in ``nl.json`` today
    (single-letter unit abbreviations that coincide between the two
    languages, an established loanword, and the app's proper-noun name) —
    anything else identical is flagged as likely-untranslated."""
    identical = sorted(
        key
        for key, en_value in _EN_STRINGS.items()
        if key not in _LEGITIMATELY_IDENTICAL_KEYS and _NL_STRINGS.get(key) == en_value
    )
    assert identical == [], f"nl.json values identical to en.json (likely untranslated): {identical}"


# --- translate(): representative keys actually resolve through the real loader ---


def test_translate_resolves_a_representative_key_to_real_dutch_text() -> None:
    result = translate("public.landing.doors_time_label", "nl")
    assert result == "Zaal open"
    assert result != translate("public.landing.doors_time_label", "en")


def test_translate_resolves_checkout_heading_to_dutch() -> None:
    assert translate("public.checkout.heading", "nl") == "Uw gegevens"


def test_translate_resolves_order_confirmation_title_to_dutch() -> None:
    assert translate("public.order_confirmation.title", "nl") == "Bestelling ontvangen"


def test_translate_resolves_sold_out_badge_to_dutch() -> None:
    assert translate("public.landing.sold_out", "nl") == "Uitverkocht"


def test_translate_dutch_result_is_not_the_raw_key() -> None:
    for key in (
        "public.checkout.submit_button",
        "public.landing.countdown_now_live",
        "public.order_confirmation.pending_notice",
    ):
        assert translate(key, "nl") != key


def test_translate_dutch_result_is_not_silently_falling_back_to_english() -> None:
    for key in (
        "public.checkout.submit_button",
        "public.landing.countdown_now_live",
        "public.order_confirmation.pending_notice",
    ):
        assert translate(key, "nl") != translate(key, "en")


def test_translate_falls_back_to_english_for_unsupported_locale() -> None:
    assert translate("public.checkout.heading", "fr") == translate("public.checkout.heading", "en")


def test_translate_falls_back_to_raw_key_when_missing_from_both_locales() -> None:
    assert translate("public.this.key.does.not.exist", "nl") == "public.this.key.does.not.exist"

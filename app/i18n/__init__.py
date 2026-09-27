"""Key-based translations loaded from ``<locale>.json`` (EN and NL)."""

import json
from functools import lru_cache
from pathlib import Path

_LOCALES_DIR = Path(__file__).parent
SUPPORTED_LOCALES = ("en", "nl")
DEFAULT_LOCALE = "en"


@lru_cache
def _load_locale(locale: str) -> dict[str, str]:
    path = _LOCALES_DIR / f"{locale}.json"
    if not path.exists():
        return {}
    data: dict[str, str] = json.loads(path.read_text(encoding="utf-8"))
    return data


def translate(key: str, locale: str = DEFAULT_LOCALE) -> str:
    """Translate ``key``, falling back to English, then to the raw key —
    so a missing string shows up visibly instead of raising."""
    strings = _load_locale(locale if locale in SUPPORTED_LOCALES else DEFAULT_LOCALE)
    if key in strings:
        return strings[key]
    default_strings = _load_locale(DEFAULT_LOCALE)
    return default_strings.get(key, key)

"""i18n scaffolding: key-based JSON string dictionaries per locale.

Per ``PROJECT_BRIEF.md``, EN and NL must be supported from Milestone 0
onward, not retrofitted later. This module wires the loading mechanism;
``content-i18n`` owns populating actual translation strings and
``frontend-theming``/``backend-builder`` own wiring ``translate()`` into
Jinja2 templates and email/PDF rendering.
"""

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
    """Resolve ``key`` to a translated string for ``locale``.

    Falls back to ``DEFAULT_LOCALE`` and then to the raw key itself if no
    translation is found, so missing strings degrade visibly rather than
    raising.
    """
    strings = _load_locale(locale if locale in SUPPORTED_LOCALES else DEFAULT_LOCALE)
    if key in strings:
        return strings[key]
    default_strings = _load_locale(DEFAULT_LOCALE)
    return default_strings.get(key, key)

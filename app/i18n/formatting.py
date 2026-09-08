"""Locale-aware date/time/currency formatting for public-site templates.

Per ``PROJECT_BRIEF.md``'s Internationalization section: "Date/time and
currency formatting follow the selected locale (e.g. '18 december 2026,
20:00' vs 'December 18, 2026, 8:00 PM')". Values arriving in public
template contexts are already-decoded JSON from the internal public API
(``app.web.public_api_client``) — ISO date/time strings and decimal-as-
string prices, not native Python ``date``/``time``/``Decimal`` objects —
so every formatter here accepts either the ISO string form or the native
type and normalizes internally, rather than requiring any route/schema
change upstream just to feed a template filter.

Deliberately hand-rolled (no ``babel`` dependency) since only two locales
are supported at all (``app.i18n.SUPPORTED_LOCALES``) and the brief's
worked example gives the exact target output for each — pulling in a full
CLDR-backed library would be more machinery than two fixed formats need.
Registered as Jinja filters in ``app.core.public_templating``.
"""

from datetime import date as date_type
from datetime import time as time_type
from decimal import Decimal, InvalidOperation

_MONTH_NAMES: dict[str, tuple[str, ...]] = {
    "en": (
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ),
    "nl": (
        "januari", "februari", "maart", "april", "mei", "juni",
        "juli", "augustus", "september", "oktober", "november", "december",
    ),
}


def _coerce_date(value: date_type | str) -> date_type:
    """Normalize an ISO ``YYYY-MM-DD`` string (as returned by the public
    JSON API) or an already-native ``date`` into a ``date``."""
    return value if isinstance(value, date_type) else date_type.fromisoformat(value)


def _coerce_time(value: time_type | str) -> time_type:
    """Normalize an ISO ``HH:MM[:SS]`` string or an already-native
    ``time`` into a ``time``."""
    return value if isinstance(value, time_type) else time_type.fromisoformat(value)


def format_date(value: date_type | str, locale: str) -> str:
    """Format a date per locale convention: ``"18 december 2026"`` (nl)
    vs ``"December 18, 2026"`` (en) — the exact pairing
    ``PROJECT_BRIEF.md``'s Internationalization section gives as its
    worked example. Falls back to the English month names/order for any
    locale other than ``"nl"``.
    """
    resolved = _coerce_date(value)
    month_name = _MONTH_NAMES.get(locale, _MONTH_NAMES["en"])[resolved.month - 1]
    if locale == "nl":
        return f"{resolved.day} {month_name} {resolved.year}"
    return f"{month_name} {resolved.day}, {resolved.year}"


def format_time(value: time_type | str, locale: str) -> str:
    """Format a time per locale convention: 24-hour ``"20:00"`` (nl) vs
    12-hour ``"8:00 PM"`` (en, no leading zero on the hour) — matches
    ``PROJECT_BRIEF.md``'s worked example.
    """
    resolved = _coerce_time(value)
    if locale == "nl":
        return f"{resolved.hour:02d}:{resolved.minute:02d}"
    hour_12 = resolved.hour % 12 or 12
    suffix = "AM" if resolved.hour < 12 else "PM"
    return f"{hour_12}:{resolved.minute:02d} {suffix}"


def format_datetime(date_value: date_type | str, time_value: time_type | str, locale: str) -> str:
    """Combine :func:`format_date` and :func:`format_time` into one
    locale-appropriate string, e.g. ``"18 december 2026, 20:00"`` (nl) or
    ``"December 18, 2026, 8:00 PM"`` (en) — verbatim the pairing
    ``PROJECT_BRIEF.md``'s Internationalization section uses as its
    example. Not currently used by any Milestone 2 template (the landing
    page shows date and time in separate slots — see
    ``app/templates/public/landing.html``'s show picker and doors/start
    time labels) but kept available for email/PDF copy in later
    milestones, where a single combined "show date" line is the natural
    shape.
    """
    return f"{format_date(date_value, locale)}, {format_time(time_value, locale)}"


def format_currency(value: Decimal | str | float, locale: str) -> str:
    """Format a EUR amount per locale numeral convention: ``"€ 15,00"``
    (nl — comma decimal separator, space after the symbol) vs
    ``"€15.00"`` (en — period decimal separator, no space). Currency
    itself is EUR-only per this milestone's known assumption (no
    multi-currency support in the data model yet, see
    ``app.web.public_context.build_event_json_ld``'s docstring); only the
    NUMBER formatting convention varies by locale, per
    ``PROJECT_BRIEF.md``'s Internationalization section.

    Accepts ``Decimal``, a numeric string (the shape the public JSON API
    actually returns for ``TicketType.price``/``Order.total``), or a bare
    ``float``/``int``. An unparseable value renders as zero rather than
    raising, so a template never 500s over a display-only formatting
    concern.
    """
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation:
        amount = Decimal(0)

    quantized = amount.quantize(Decimal("0.01"))
    sign = "-" if quantized < 0 else ""
    whole, _, fraction = f"{abs(quantized):.2f}".partition(".")
    grouped_whole = f"{int(whole):,}"  # thousands separator for a large group order total

    if locale == "nl":
        grouped_whole = grouped_whole.replace(",", ".")
        return f"€ {sign}{grouped_whole},{fraction}"
    return f"€{sign}{grouped_whole}.{fraction}"

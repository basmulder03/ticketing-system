"""Locale-aware date/time/currency Jinja filters (EN and NL).

Accept both native values and the ISO/decimal strings the public JSON API
returns. Hand-rolled rather than using ``babel``: two fixed locale formats
don't justify a CLDR dependency.
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
    """ISO ``YYYY-MM-DD`` string or ``date`` -> ``date``."""
    return value if isinstance(value, date_type) else date_type.fromisoformat(value)


def _coerce_time(value: time_type | str) -> time_type:
    """ISO ``HH:MM[:SS]`` string or ``time`` -> ``time``."""
    return value if isinstance(value, time_type) else time_type.fromisoformat(value)


def format_date(value: date_type | str, locale: str) -> str:
    """``"18 december 2026"`` (nl) / ``"December 18, 2026"`` (other)."""
    resolved = _coerce_date(value)
    month_name = _MONTH_NAMES.get(locale, _MONTH_NAMES["en"])[resolved.month - 1]
    if locale == "nl":
        return f"{resolved.day} {month_name} {resolved.year}"
    return f"{month_name} {resolved.day}, {resolved.year}"


def format_time(value: time_type | str, locale: str) -> str:
    """``"20:00"`` (nl) / ``"8:00 PM"`` (other)."""
    resolved = _coerce_time(value)
    if locale == "nl":
        return f"{resolved.hour:02d}:{resolved.minute:02d}"
    hour_12 = resolved.hour % 12 or 12
    suffix = "AM" if resolved.hour < 12 else "PM"
    return f"{hour_12}:{resolved.minute:02d} {suffix}"


def format_datetime(date_value: date_type | str, time_value: time_type | str, locale: str) -> str:
    """``"18 december 2026, 20:00"`` (nl) / ``"December 18, 2026, 8:00 PM"``."""
    return f"{format_date(date_value, locale)}, {format_time(time_value, locale)}"


def format_currency(value: Decimal | str | float, locale: str) -> str:
    """EUR amount: ``"€ 15,00"`` (nl) / ``"€15.00"`` (other).

    An unparseable value renders as zero rather than raising — a display
    filter should never 500 a page.
    """
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation:
        amount = Decimal(0)

    quantized = amount.quantize(Decimal("0.01"))
    sign = "-" if quantized < 0 else ""
    whole, _, fraction = f"{abs(quantized):.2f}".partition(".")
    grouped_whole = f"{int(whole):,}"

    if locale == "nl":
        grouped_whole = grouped_whole.replace(",", ".")
        return f"€ {sign}{grouped_whole},{fraction}"
    return f"€{sign}{grouped_whole}.{fraction}"

"""Unit tests for ``app.i18n.formatting``: the locale-aware date/time/
currency Jinja filters behind Milestone 2's i18n requirement.

Per ``PROJECT_BRIEF.md``'s Internationalization section: "Date/time and
currency formatting follow the selected locale (e.g. '18 december 2026,
20:00' vs 'December 18, 2026, 8:00 PM')" — every case here is anchored to
that worked example or to behavior explicitly documented in each
formatter's own docstring. Pure functions, no DB/app context needed.
Expected values for the thousands-grouping/negative-amount/unparseable-
input cases were derived by running the real function, not guessed, per
this project's test convention (see ``tests/unit/test_css_sanitizer.py``).
"""

from datetime import date, time
from decimal import Decimal

from app.i18n.formatting import format_currency, format_date, format_datetime, format_time

# --- format_date ------------------------------------------------------------


def test_format_date_matches_the_briefs_worked_example_nl() -> None:
    assert format_date(date(2026, 12, 18), "nl") == "18 december 2026"


def test_format_date_matches_the_briefs_worked_example_en() -> None:
    assert format_date(date(2026, 12, 18), "en") == "December 18, 2026"


def test_format_date_accepts_iso_string_nl() -> None:
    assert format_date("2026-12-18", "nl") == "18 december 2026"


def test_format_date_accepts_iso_string_en() -> None:
    assert format_date("2026-12-18", "en") == "December 18, 2026"


def test_format_date_accepts_native_date_object() -> None:
    assert format_date(date(2026, 1, 1), "nl") == "1 januari 2026"


def test_format_date_falls_back_to_english_month_names_and_order_for_unknown_locale() -> None:
    """A locale other than ``"nl"`` (e.g. an unsupported browser-inferred
    code that slipped past ``resolve_locale``) must render as English, not
    raise and not silently emit a blank/garbled month name."""
    assert format_date(date(2026, 12, 18), "fr") == "December 18, 2026"


# --- format_time --------------------------------------------------------


def test_format_time_matches_the_briefs_worked_example_nl() -> None:
    assert format_time(time(20, 0), "nl") == "20:00"


def test_format_time_matches_the_briefs_worked_example_en() -> None:
    assert format_time(time(20, 0), "en") == "8:00 PM"


def test_format_time_midnight_en_is_twelve_am_not_zero() -> None:
    assert format_time(time(0, 0), "en") == "12:00 AM"


def test_format_time_noon_en_is_twelve_pm_not_zero() -> None:
    assert format_time(time(12, 0), "en") == "12:00 PM"


def test_format_time_midnight_nl_is_zero_zero_hundred_hours() -> None:
    assert format_time(time(0, 0), "nl") == "00:00"


def test_format_time_accepts_iso_string() -> None:
    assert format_time("20:00", "nl") == "20:00"
    assert format_time("20:00", "en") == "8:00 PM"


def test_format_time_accepts_iso_string_with_seconds() -> None:
    assert format_time("20:00:00", "nl") == "20:00"


def test_format_time_accepts_native_time_object() -> None:
    assert format_time(time(8, 5), "en") == "8:05 AM"


# --- format_datetime ------------------------------------------------------


def test_format_datetime_combines_date_and_time_nl() -> None:
    assert format_datetime(date(2026, 12, 18), time(20, 0), "nl") == "18 december 2026, 20:00"


def test_format_datetime_combines_date_and_time_en() -> None:
    assert format_datetime(date(2026, 12, 18), time(20, 0), "en") == "December 18, 2026, 8:00 PM"


def test_format_datetime_accepts_iso_strings() -> None:
    assert format_datetime("2026-12-18", "20:00", "nl") == "18 december 2026, 20:00"


# --- format_currency ------------------------------------------------------


def test_format_currency_matches_the_briefs_worked_example_nl() -> None:
    assert format_currency(Decimal("15.00"), "nl") == "€ 15,00"


def test_format_currency_matches_the_briefs_worked_example_en() -> None:
    assert format_currency(Decimal("15.00"), "en") == "€15.00"


def test_format_currency_accepts_numeric_string() -> None:
    assert format_currency("15.00", "nl") == "€ 15,00"
    assert format_currency("15.00", "en") == "€15.00"


def test_format_currency_accepts_float() -> None:
    assert format_currency(15.5, "en") == "€15.50"


def test_format_currency_accepts_int() -> None:
    assert format_currency(15, "en") == "€15.00"


def test_format_currency_thousands_grouping_nl_uses_period_separator() -> None:
    """NL swaps the roles of ``,``/``.`` entirely relative to EN: the
    thousands separator becomes a period (not a comma) once the decimal
    separator itself is a comma."""
    assert format_currency(Decimal("12345.67"), "nl") == "€ 12.345,67"


def test_format_currency_thousands_grouping_en_uses_comma_separator() -> None:
    assert format_currency(Decimal("12345.67"), "en") == "€12,345.67"


def test_format_currency_negative_amount_nl_places_sign_after_the_space() -> None:
    """Not explicitly documented in the docstring, but pinned here since
    the implementation clearly special-cases sign placement (a Decimal's
    own default ``str()`` would put the ``-`` before the ``€``, not after
    the NL space) — a refund/credit line is a realistic future caller."""
    assert format_currency(Decimal("-15.00"), "nl") == "€ -15,00"


def test_format_currency_negative_amount_en() -> None:
    assert format_currency(Decimal("-15.00"), "en") == "€-15.00"


def test_format_currency_unparseable_string_falls_back_to_zero_rather_than_raising() -> None:
    """Per the docstring: 'An unparseable value renders as zero rather than
    raising, so a template never 500s over a display-only formatting
    concern.'"""
    assert format_currency("not-a-number", "en") == "€0.00"
    assert format_currency("not-a-number", "nl") == "€ 0,00"

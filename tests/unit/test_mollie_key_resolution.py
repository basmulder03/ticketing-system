"""Unit tests for ``app.services.mollie.resolve_mollie_api_key`` — pure
logic, no DB/network involved (an ``EventConfig`` instance can be
constructed directly without a session; SQLAlchemy declarative models don't
require persistence to exist as plain Python objects).

Covers PROJECT_BRIEF.md's Payments requirement: "Backoffice setting for
test vs. live Mollie API key, per event, kept separate per environment" —
the explicit-choice, no-fallback behavior documented on the function itself.
"""

from app.models.enums import MollieMode
from app.models.event_config import EventConfig
from app.services.mollie import resolve_mollie_api_key


def _config(**kwargs: object) -> EventConfig:
    """Build a bare, unpersisted ``EventConfig`` with sane defaults for the
    fields ``resolve_mollie_api_key`` doesn't care about."""
    defaults: dict[str, object] = {
        "mollie_mode": MollieMode.TEST,
        "mollie_test_api_key": None,
        "mollie_live_api_key": None,
    }
    defaults.update(kwargs)
    return EventConfig(**defaults)


def test_none_config_returns_none() -> None:
    assert resolve_mollie_api_key(None) is None


def test_test_mode_uses_test_key_even_when_live_key_is_also_set() -> None:
    config = _config(
        mollie_mode=MollieMode.TEST,
        mollie_test_api_key="test_abc123",
        mollie_live_api_key="live_xyz789",
    )
    assert resolve_mollie_api_key(config) == "test_abc123"


def test_live_mode_uses_live_key() -> None:
    config = _config(
        mollie_mode=MollieMode.LIVE,
        mollie_test_api_key="test_abc123",
        mollie_live_api_key="live_xyz789",
    )
    assert resolve_mollie_api_key(config) == "live_xyz789"


def test_test_mode_with_no_test_key_returns_none_even_though_live_key_is_set() -> None:
    """Never falls back to the other mode's key — a misconfigured test-mode
    event with only a live key set must resolve to no key at all, not
    silently start charging real cards."""
    config = _config(
        mollie_mode=MollieMode.TEST,
        mollie_test_api_key=None,
        mollie_live_api_key="live_xyz789",
    )
    assert resolve_mollie_api_key(config) is None


def test_live_mode_with_no_live_key_returns_none_even_though_test_key_is_set() -> None:
    config = _config(
        mollie_mode=MollieMode.LIVE,
        mollie_test_api_key="test_abc123",
        mollie_live_api_key=None,
    )
    assert resolve_mollie_api_key(config) is None


def test_empty_string_key_is_treated_as_unset() -> None:
    """An empty string (e.g. a cleared-but-not-null field) is treated the
    same as ``None`` — never returned as a usable key."""
    config = _config(mollie_mode=MollieMode.TEST, mollie_test_api_key="")
    assert resolve_mollie_api_key(config) is None

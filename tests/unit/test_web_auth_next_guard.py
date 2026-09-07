"""Unit tests for ``app.web.routes.auth._safe_next``: the open-redirect
guard behind the backoffice login flow's ``?next=`` parameter.

Pure function, no request/DB context needed — the HTTP-level behavior
(actually redirecting there) is covered in
``tests/integration/test_web_auth_routes.py``.
"""

from app.web.routes.auth import _safe_next


def test_relative_path_is_allowed() -> None:
    assert _safe_next("/events/abc-123/theme") == "/events/abc-123/theme"


def test_root_relative_path_is_allowed() -> None:
    assert _safe_next("/events") == "/events"


def test_absolute_url_falls_back_to_events() -> None:
    assert _safe_next("https://evil.example.com") == "/events"


def test_absolute_http_url_falls_back_to_events() -> None:
    assert _safe_next("http://evil.example.com/steal") == "/events"


def test_protocol_relative_url_falls_back_to_events() -> None:
    assert _safe_next("//evil.example.com") == "/events"


def test_bare_domain_without_scheme_falls_back_to_events() -> None:
    assert _safe_next("evil.example.com") == "/events"


def test_empty_string_falls_back_to_events() -> None:
    assert _safe_next("") == "/events"

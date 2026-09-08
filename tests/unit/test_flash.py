"""Unit tests for ``app.web.flash.redirect_with_flash``.

Pure function, no request/DB context needed — the double-``?`` bug this
guards against (see the function's own docstring) was caught during
Milestone 4's live testing on the EmailTemplate editor's
``?language=en``-carrying redirect path: a naive ``f"{path}?flash=..."``
would have produced ``...?language=en?flash=...``, which most URL parsers
(including the browser navigating the ``Location`` header) treat as a
single opaque query string where ``flash`` never becomes its own key — so
the flash banner silently never rendered.
"""

from urllib.parse import parse_qs, urlsplit

from app.web.flash import redirect_with_flash


def test_path_without_query_string_gets_a_leading_question_mark() -> None:
    response = redirect_with_flash("/events/abc/orders", "Done.", kind="success")

    parsed = urlsplit(response.headers["location"])
    assert parsed.path == "/events/abc/orders"
    query = parse_qs(parsed.query)
    assert query["flash"] == ["Done."]
    assert query["flash_kind"] == ["success"]


def test_path_with_existing_query_string_gets_ampersand_not_a_second_question_mark() -> None:
    response = redirect_with_flash(
        "/events/abc/email-templates?language=en", "Email template saved.", kind="success"
    )

    location = response.headers["location"]
    # The regression this guards: a naive implementation would produce
    # "...?language=en?flash=...", a single query string where "flash" is
    # never parsed out as its own key.
    assert location.count("?") == 1
    parsed = urlsplit(location)
    assert parsed.path == "/events/abc/email-templates"
    query = parse_qs(parsed.query)
    assert query["language"] == ["en"]
    assert query["flash"] == ["Email template saved."]
    assert query["flash_kind"] == ["success"]


def test_error_kind_is_reflected_in_the_query_string() -> None:
    response = redirect_with_flash("/events/abc/orders", "Order not found.", kind="error")

    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["flash_kind"] == ["error"]


def test_message_is_url_encoded() -> None:
    response = redirect_with_flash("/events/abc/orders", "a & b?", kind="success")

    # Raw '&'/'?' in the message must not be interpretable as new query
    # string separators once encoded.
    location = response.headers["location"]
    assert "a & b?" not in location
    query = parse_qs(urlsplit(location).query)
    assert query["flash"] == ["a & b?"]


def test_status_code_is_303_see_other() -> None:
    response = redirect_with_flash("/events/abc/orders", "Done.", kind="success")
    assert response.status_code == 303

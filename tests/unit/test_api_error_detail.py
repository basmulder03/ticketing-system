"""Unit tests for ``app.web.api_client.api_error_detail``."""

import httpx

from app.web.api_client import api_error_detail


def test_plain_string_detail_is_returned_as_is() -> None:
    response = httpx.Response(409, json={"detail": "Slug already exists."})
    assert api_error_detail(response, "fallback") == "Slug already exists."


def test_pydantic_422_list_becomes_readable_field_messages() -> None:
    response = httpx.Response(
        422,
        json={
            "detail": [
                {"loc": ["body", "capacity"], "msg": "Input should be greater than 0"},
                {"loc": ["body"], "msg": "Field required"},
            ]
        },
    )
    assert api_error_detail(response, "fallback") == "capacity: Input should be greater than 0; Field required"


def test_non_json_or_unexpected_shapes_fall_back() -> None:
    assert api_error_detail(httpx.Response(500, text="Internal Server Error"), "fallback") == "fallback"
    assert api_error_detail(httpx.Response(400, json=["not", "a", "dict"]), "fallback") == "fallback"
    assert api_error_detail(httpx.Response(400, json={"detail": 42}), "fallback") == "fallback"

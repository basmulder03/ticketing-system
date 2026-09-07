"""Unit tests for ``app.api.routes._utils``: ``apply_partial_update`` and
``parse_uuid_or_404``. Both are pure functions (no DB, no FastAPI request
context needed to exercise them), so they're covered here rather than only
indirectly via the integration route tests — this is the isolable
business-logic layer behind every Milestone 1 PATCH/PUT route's "only
fields explicitly present in the body are applied" semantics.
"""

import uuid

import pytest
from fastapi import HTTPException
from pydantic import BaseModel

from app.api.routes._utils import apply_partial_update, parse_uuid_or_404


class _DummyInstance:
    def __init__(self, name: str, count: int) -> None:
        self.name = name
        self.count = count


class _DummyUpdateBody(BaseModel):
    name: str | None = None
    count: int | None = None


def test_apply_partial_update_only_applies_explicitly_present_fields() -> None:
    instance = _DummyInstance(name="original", count=1)
    body = _DummyUpdateBody(name="updated")  # count omitted entirely

    changes = apply_partial_update(instance, body)

    assert instance.name == "updated"
    assert instance.count == 1  # untouched
    assert changes == {"name": "updated"}


def test_apply_partial_update_with_no_fields_set_is_a_no_op() -> None:
    instance = _DummyInstance(name="original", count=1)
    body = _DummyUpdateBody()

    changes = apply_partial_update(instance, body)

    assert instance.name == "original"
    assert instance.count == 1
    assert changes == {}


def test_apply_partial_update_explicit_none_is_applied_not_ignored() -> None:
    """An explicit ``null`` in the request body IS "set" from Pydantic's
    point of view (``exclude_unset`` only excludes keys absent from the
    input entirely) — so it overwrites the field with ``None``, distinct
    from simply omitting the key."""
    instance = _DummyInstance(name="original", count=1)
    body = _DummyUpdateBody.model_construct(_fields_set={"name"}, name=None, count=None)

    changes = apply_partial_update(instance, body)

    assert instance.name is None
    assert changes == {"name": None}


def test_apply_partial_update_returns_every_applied_field() -> None:
    instance = _DummyInstance(name="original", count=1)
    body = _DummyUpdateBody(name="updated", count=99)

    changes = apply_partial_update(instance, body)

    assert changes == {"name": "updated", "count": 99}


def test_parse_uuid_or_404_returns_the_parsed_uuid() -> None:
    valid = str(uuid.uuid4())

    parsed = parse_uuid_or_404(valid, detail="not found")

    assert str(parsed) == valid


def test_parse_uuid_or_404_raises_404_not_400_for_malformed_input() -> None:
    with pytest.raises(HTTPException) as exc_info:
        parse_uuid_or_404("not-a-uuid", detail="Event not found.")

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Event not found."


def test_parse_uuid_or_404_rejects_empty_string() -> None:
    with pytest.raises(HTTPException) as exc_info:
        parse_uuid_or_404("", detail="not found")

    assert exc_info.value.status_code == 404

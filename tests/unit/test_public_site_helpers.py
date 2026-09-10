"""Unit tests for the pure, private helpers in
``app.web.routes.public_site``: the ``qty_<ticket_type_id>`` form-field
parser and the ``CheckoutError`` -> translated-message mapper. Both are
plain functions with no Request/DB dependency, so they're tested directly
here rather than only indirectly through a full HTTP round trip (see
``tests/integration/test_public_site_web_routes.py`` for the end-to-end
form-submission behavior). Mirrors the existing
``tests/unit/test_web_auth_next_guard.py`` convention of unit-testing a
route module's underscore-prefixed pure helpers directly.
"""

from app.web.routes.public_site import (
    _find_show,
    _parse_checkout_items,
    _sellable_shows,
    _translate_checkout_error,
)


def _event(shows: list[dict[str, object]]) -> dict[str, object]:
    return {"shows": shows}


def _show(show_id: str, ticket_type_ids: list[str]) -> dict[str, object]:
    return {"id": show_id, "ticket_types": [{"id": tid} for tid in ticket_type_ids]}


# --- _find_show ------------------------------------------------------


def test_find_show_returns_matching_show() -> None:
    event = _event([_show("s1", []), _show("s2", [])])
    found = _find_show(event, "s2")
    assert found is not None
    assert found["id"] == "s2"


def test_find_show_returns_none_for_unknown_id() -> None:
    event = _event([_show("s1", [])])
    assert _find_show(event, "does-not-exist") is None


def test_find_show_returns_none_for_none_id() -> None:
    event = _event([_show("s1", [])])
    assert _find_show(event, None) is None


# --- _sellable_shows -----------------------------------------------------


def test_sellable_shows_drops_shows_with_no_ticket_types() -> None:
    event = _event([_show("s1", ["tt1"]), _show("s2", [])])
    assert [s["id"] for s in _sellable_shows(event)] == ["s1"]


def test_sellable_shows_keeps_every_show_when_all_have_ticket_types() -> None:
    event = _event([_show("s1", ["tt1"]), _show("s2", ["tt2", "tt3"])])
    assert [s["id"] for s in _sellable_shows(event)] == ["s1", "s2"]


def test_sellable_shows_returns_empty_list_when_none_are_sellable() -> None:
    event = _event([_show("s1", []), _show("s2", [])])
    assert _sellable_shows(event) == []


# --- _parse_checkout_items ---------------------------------------------


def test_parse_checkout_items_reads_qty_fields_for_selected_show() -> None:
    event = _event([_show("s1", ["tt-1", "tt-2"])])
    form = {"qty_tt-1": "2", "qty_tt-2": "0"}
    items = _parse_checkout_items(event, form, "s1")
    assert items == [{"ticket_type_id": "tt-1", "quantity": 2}]


def test_parse_checkout_items_ignores_fields_for_a_non_selected_show() -> None:
    """A JS-disabled browser submits every show panel's fields, but only the
    buyer-selected show's quantities should ever be honored — see the
    function's own docstring."""
    event = _event([_show("s1", ["tt-1"]), _show("s2", ["tt-2"])])
    form = {"qty_tt-1": "0", "qty_tt-2": "3"}
    items = _parse_checkout_items(event, form, "s1")
    assert items == []


def test_parse_checkout_items_returns_empty_list_for_unknown_show() -> None:
    event = _event([_show("s1", ["tt-1"])])
    assert _parse_checkout_items(event, {"qty_tt-1": "2"}, "no-such-show") == []


def test_parse_checkout_items_treats_missing_field_as_zero() -> None:
    event = _event([_show("s1", ["tt-1", "tt-2"])])
    items = _parse_checkout_items(event, {"qty_tt-1": "1"}, "s1")
    assert items == [{"ticket_type_id": "tt-1", "quantity": 1}]


def test_parse_checkout_items_treats_non_numeric_value_as_zero_not_an_error() -> None:
    event = _event([_show("s1", ["tt-1"])])
    items = _parse_checkout_items(event, {"qty_tt-1": "not-a-number"}, "s1")
    assert items == []


def test_parse_checkout_items_excludes_negative_quantities() -> None:
    event = _event([_show("s1", ["tt-1"])])
    items = _parse_checkout_items(event, {"qty_tt-1": "-5"}, "s1")
    assert items == []


def test_parse_checkout_items_all_zero_quantities_yields_empty_list() -> None:
    event = _event([_show("s1", ["tt-1", "tt-2"])])
    items = _parse_checkout_items(event, {"qty_tt-1": "0", "qty_tt-2": "0"}, "s1")
    assert items == []


# --- _translate_checkout_error ------------------------------------------


def test_translate_checkout_error_404_maps_to_unavailable() -> None:
    assert _translate_checkout_error(404, "This event is not currently available.", "en") == (
        "This event is not currently available."
    )


def test_translate_checkout_error_403_paused_maps_to_sales_paused_message() -> None:
    message = _translate_checkout_error(403, "Sales are currently paused for this event.", "en")
    assert message == "Sales are currently paused for this event. Please check back later."


def test_translate_checkout_error_403_not_live_maps_to_sales_not_live_message() -> None:
    message = _translate_checkout_error(403, "Sales are not live yet for this event.", "en")
    assert message == "Tickets are not on sale yet for this event."


def test_translate_checkout_error_409_maps_to_sold_out_message() -> None:
    message = _translate_checkout_error(409, "Only 1 ticket(s) remain for one of the requested ticket types.", "en")
    assert message == "Sorry, not enough tickets remain for one of the ticket types you selected."


def test_translate_checkout_error_distinct_messages_for_each_mapped_reason() -> None:
    """The four explicitly-mapped rejection reasons must never collapse to
    the same string as each other (the whole point of this mapper, per
    PROJECT_BRIEF.md/this milestone's commit message)."""
    messages = {
        _translate_checkout_error(404, "This event is not currently available.", "en"),
        _translate_checkout_error(403, "Sales are currently paused for this event.", "en"),
        _translate_checkout_error(403, "Sales are not live yet for this event.", "en"),
        _translate_checkout_error(409, "Only 1 ticket(s) remain...", "en"),
    }
    assert len(messages) == 4


def test_translate_checkout_error_payment_method_not_enabled_gets_its_own_message() -> None:
    """``PaymentMethodNotEnabledCheckoutError`` (HTTP 422) previously fell
    through to the generic message since its detail text didn't contain
    "paused"/"not live" — fixed by matching on "payment method" in the
    (lowercased) detail text, same brittleness-acknowledged approach as the
    other status/text-matched branches (see this function's docstring for
    why a proper error_code would be better)."""
    detail = "Payment method 'mollie' is not enabled for this event."
    message = _translate_checkout_error(422, detail, "en")
    assert message == "That payment method isn't available for this event. Please choose another."
    assert message != "We couldn't process your order. Please check your details and try again."


def test_translate_checkout_error_unmapped_status_falls_back_to_generic() -> None:
    assert _translate_checkout_error(500, "boom", "en") == (
        "We couldn't process your order. Please check your details and try again."
    )

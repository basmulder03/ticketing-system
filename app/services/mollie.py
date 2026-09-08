"""Mollie payment integration: creating a Mollie-hosted payment for an
Order, and fetching a payment's authoritative status directly from Mollie's
API. Plain ``httpx`` calls, no Mollie SDK dependency — matches the existing
connection-test pattern in ``app.services.event_config_actions``.

**On webhook "signature verification"** (read this before touching the
webhook handler in ``app.api.routes.public``): Mollie's real webhook model
sends only a payment id, form-encoded as ``id=tr_xxx`` — there is no signed
payload, no HMAC header, nothing in the webhook request itself that can be
cryptographically verified. This is normal for Mollie, not a shortcut this
project is taking. The correct, secure pattern (and what Mollie's own docs
mean by "verifying" a webhook) is: never trust anything about payment
*status* from the webhook body — treat the ``id`` it names purely as a
lookup key, then fetch that payment's current state directly from Mollie's
API using the merchant's own API key via :func:`fetch_mollie_payment_status`.
That authoritated response, not the webhook POST, is what gets trusted and
reconciled against. Anyone can POST a fake ``id`` to the webhook endpoint;
the worst they can do is force a wasted lookup (an unknown id resolves to no
matching Order, see the webhook route) or a redundant Mollie GET call for a
real order they don't control the outcome of.
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal

import httpx

from app.models.enums import MollieMode
from app.models.event_config import EventConfig
from app.services.event_config_actions import MOLLIE_API_BASE_URL

_HTTP_TIMEOUT_SECONDS = 10.0

_CURRENCY = "EUR"
"""Fixed at EUR — EventConfig has no currency field and every event this
platform targets (Dutch/EU community venues) bills in EUR. Flagged as an
assumption: a future multi-currency requirement would need a real
``EventConfig`` field, not a hardcoded constant here."""


class MollieApiError(Exception):
    """Raised when a Mollie API call can't be completed: network failure,
    an unexpected non-2xx response, or a response that doesn't have the
    shape this code expects. Callers must catch this and degrade
    gracefully (a failed checkout error, or a 502 from the webhook route
    so Mollie retries) — never let it surface as a bare 500."""


def resolve_mollie_api_key(config: EventConfig | None) -> str | None:
    """Return the API key matching ``config.mollie_mode`` (test or live).

    Reads ``mollie_mode`` from this specific Event's own config — never a
    global default — per PROJECT_BRIEF.md's Per-Event Configuration
    requirement. Never falls back to the other mode's key if the selected
    one isn't set: which key is live for this event is an explicit admin
    choice, not something to infer opportunistically. Returns ``None`` if
    ``config`` is ``None`` or the selected mode's key isn't configured.
    """
    if config is None:
        return None
    key = config.mollie_live_api_key if config.mollie_mode == MollieMode.LIVE else config.mollie_test_api_key
    return key or None


@dataclass(frozen=True)
class MolliePaymentCreated:
    """The two fields this app needs back from a successful Mollie
    Create Payment call."""

    payment_id: str
    checkout_url: str


async def create_mollie_payment(
    *,
    api_key: str,
    order_id: uuid.UUID,
    amount: Decimal,
    redirect_url: str,
    webhook_url: str,
    description: str,
) -> MolliePaymentCreated:
    """Call Mollie's ``POST /v2/payments`` for ``amount`` (the Order's
    total). Raises :class:`MollieApiError` on any network failure or
    non-2xx response — the caller must fail the checkout cleanly rather
    than send the buyer to a broken redirect.

    Never logs ``api_key`` or the raw response body (which could echo
    metadata back) — only structured fields are read out of it.
    """
    payload = {
        "amount": {"currency": _CURRENCY, "value": f"{amount:.2f}"},
        "description": description,
        "redirectUrl": redirect_url,
        "webhookUrl": webhook_url,
        "metadata": {"order_id": str(order_id)},
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{MOLLIE_API_BASE_URL}/payments",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise MollieApiError(f"Could not reach Mollie: {exc}") from exc

    if response.status_code not in (200, 201):
        raise MollieApiError(f"Mollie rejected the payment request (HTTP {response.status_code}).")

    data = response.json()
    try:
        payment_id = data["id"]
        checkout_url = data["_links"]["checkout"]["href"]
    except (KeyError, TypeError) as exc:
        raise MollieApiError("Unexpected response shape from Mollie's create-payment API.") from exc
    if not isinstance(payment_id, str) or not isinstance(checkout_url, str):
        raise MollieApiError("Unexpected response shape from Mollie's create-payment API.")
    return MolliePaymentCreated(payment_id=payment_id, checkout_url=checkout_url)


async def fetch_mollie_payment_status(*, api_key: str, payment_id: str) -> str:
    """Fetch ``payment_id``'s CURRENT status directly from Mollie's API —
    the only trusted source of payment status (see module docstring; never
    read status from a webhook request body).

    Returns Mollie's raw ``status`` string (e.g. ``"paid"``, ``"failed"``,
    ``"expired"``, ``"canceled"``, ``"open"``, ``"pending"``,
    ``"authorized"``) — mapping that onto ``OrderStatus`` is the caller's
    job (see ``app.services.order_payment``/the webhook route), not this
    thin client's.
    """
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(
                f"{MOLLIE_API_BASE_URL}/payments/{payment_id}",
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.HTTPError as exc:
        raise MollieApiError(f"Could not reach Mollie: {exc}") from exc

    if response.status_code != 200:
        raise MollieApiError(f"Mollie returned HTTP {response.status_code} fetching payment {payment_id}.")

    data = response.json()
    payment_status = data.get("status")
    if not isinstance(payment_status, str):
        raise MollieApiError("Unexpected response shape from Mollie's get-payment API (missing status).")
    return payment_status

"""Mollie client (plain ``httpx``, no SDK): create a payment, fetch its status.

Mollie webhooks carry only a payment id — no signature. So the webhook body
is never trusted for status: its id is a lookup key, and the status is
fetched from Mollie with our own key (:func:`fetch_mollie_payment_status`).
A forged webhook can at worst cause a wasted lookup.
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
"""EUR only; multi-currency would need an EventConfig field."""


class MollieApiError(Exception):
    """A Mollie call failed (network, non-2xx, unexpected shape). Callers must
    degrade gracefully (checkout error, or 502 so Mollie retries) — never a 500.
    """


def resolve_mollie_api_key(config: EventConfig | None, *, mode: MollieMode | None = None) -> str | None:
    """The event's key for ``mode`` (default: ``config.mollie_mode``), or ``None``.

    Never falls back to the other mode's key. The webhook passes the order's
    pinned ``Order.mollie_mode`` so a test/live switch can't strand an
    in-flight payment.
    """
    if config is None:
        return None
    resolved_mode = mode if mode is not None else config.mollie_mode
    key = config.mollie_live_api_key if resolved_mode == MollieMode.LIVE else config.mollie_test_api_key
    return key or None


@dataclass(frozen=True)
class MolliePaymentCreated:
    """The fields we need from a created payment."""

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
    """Create a payment for ``amount``. Raises :class:`MollieApiError` so checkout
    fails cleanly. Never logs the key or the raw response.
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
    """Mollie's current raw status for ``payment_id`` (``"paid"``, ``"failed"``,
    ``"open"``...) — the only trusted source. Mapping to ``OrderStatus`` is the
    caller's job.
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

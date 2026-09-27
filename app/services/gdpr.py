"""Buyer-PII erasure for an ``Order``.

Overwrites only the buyer name/email/address; the order, its tickets and
invoice stay for accounting and stats. Invoiced orders are a legal tension
(invoices must often be kept ~7 years, with name and address), so the
*route* asks the admin for an explicit ``confirm`` before erasing one —
friction, not a block. This module is the unconditional primitive.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal
from app.models.order import Order
from app.services.audit import record_audit_entry

ERASED_PLACEHOLDER = "[erased]"
"""Fixed marker for erased name/address: recognizable in the UI and exports,
and makes re-erasing a no-op.
"""


def _erased_email(order_id: uuid.UUID) -> str:
    """A per-order ``.invalid`` placeholder email (reserved, never deliverable),
    unique so erased orders stay distinguishable.
    """
    return f"erased-{order_id}@erased.invalid"


async def erase_order_pii(session: AsyncSession, *, order: Order, principal: Principal) -> Order:
    """Replace the buyer's details with placeholders and write one audit entry.

    Idempotent. The audit ``detail`` is ``None`` on purpose: logging the erased
    PII would defeat the erasure. Doesn't commit. Afterwards, resending the
    confirmation email will fail (the address is ``.invalid``) — that's correct.
    """
    order.buyer_name = ERASED_PLACEHOLDER
    order.buyer_email = _erased_email(order.id)
    order.buyer_address = ERASED_PLACEHOLDER
    await record_audit_entry(
        session,
        principal,
        action="order.pii_erased",
        target_type="Order",
        target_id=str(order.id),
        detail=None,
    )
    await session.flush()
    return order

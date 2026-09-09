"""Buyer-PII erasure for ``Order`` rows — the technical mechanism behind
PROJECT_BRIEF.md's Security & Ops requirement ("GDPR-conscious: minimal PII
retention, deletion-on-request process, privacy policy page").

Scope and what this deliberately does NOT do (read before calling into this
module): erasure targets exactly the three buyer-identifying columns on
``Order`` — ``buyer_name``/``buyer_email``/``buyer_address`` (see
``app.models.order.Order`` docstring: these are "the actual PII store in
this app"). It never deletes the ``Order`` row itself, its ``Ticket`` rows,
or its ``Invoice`` — those remain for accounting/stats integrity (ticket
counts, revenue figures, invoice records referenced by
``app.services.stats``/``app.services.invoicing``). This module performs a
data-minimization overwrite, not a right-to-be-forgotten row deletion.

Legal-retention friction, by design (this is the load-bearing decision in
this module, not an incidental detail): full unconditional PII erasure can
directly conflict with Dutch/EU accounting and tax law, which typically
requires invoices — commonly including the customer's name and address —
to be retained for a statutory period (often seven years in the
Netherlands). This module and its caller (see
``app.api.routes.orders.erase_order_pii_route``) therefore do NOT silently
block erasure of an invoiced order (that would prevent an admin from
honoring a legitimate deletion request the software has no legal standing
to second-guess), and do NOT silently allow it either (that could put the
event organizer in breach of their own invoice-retention obligations
without them ever seeing the tradeoff). Instead, the API route requires an
explicit ``confirm: true`` from the admin before erasing an Order that has
an issued Invoice — friction, not a block — while an Order with no Invoice
erases immediately, no confirmation needed. This module itself is
deliberately unaware of that confirmation gate: it is the unconditional
"do the erasure" primitive, and the invoice-retention judgment call lives
entirely at the route layer, matching this codebase's own pattern of
keeping legal/authorization gates in route/service boundaries rather than
buried inside the innermost mutation.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal
from app.models.order import Order
from app.services.audit import record_audit_entry

ERASED_PLACEHOLDER = "[erased]"
"""Fixed placeholder written to ``buyer_name``/``buyer_address`` on
erasure. Deliberately not blank/``NULL`` (both columns are
``nullable=False``) and deliberately not a randomized value — a fixed,
unambiguous marker makes an erased Order immediately recognizable in the
backoffice UI/exports, and keeps re-running erasure on an already-erased
Order a harmless no-op (see :func:`erase_order_pii`)."""


def _erased_email(order_id: uuid.UUID) -> str:
    """Build a per-Order placeholder email for ``Order.buyer_email``.

    ``Order.buyer_email`` is indexed but NOT unique at the DB layer (see
    ``app.models.order.Order`` — only ``mollie_payment_id`` carries a
    uniqueness constraint), so a single shared placeholder like
    ``"erased@erased.invalid"`` would have been schema-legal. It's avoided
    anyway: keeping the value unique per Order avoids the (admittedly
    small) foot-gun of two erased orders becoming indistinguishable by
    email alone in any future tooling/report that groups or looks up
    orders by ``buyer_email``, and reads unambiguously as "this specific
    order's buyer data was erased" rather than a real address. The
    ``.invalid`` TLD is reserved by RFC 2606 specifically for this kind of
    guaranteed-non-deliverable placeholder use.
    """
    return f"erased-{order_id}@erased.invalid"


async def erase_order_pii(session: AsyncSession, *, order: Order, principal: Principal) -> Order:
    """Overwrite ``order.buyer_name``/``buyer_email``/``buyer_address``
    with fixed anonymized placeholders and write one audit entry recording
    who erased it and when.

    Idempotent by construction: this always WRITES the same fixed
    placeholders rather than computing a delta, so calling it twice on the
    same (or an already-erased) Order is a harmless no-op that just
    re-applies identical values and appends one more (harmless, truthful)
    audit entry — it never raises or double-erases in any way that would
    matter.

    The audit entry's ``detail`` is deliberately ``None`` — it must NOT
    contain the erased buyer_name/email/address (or anything derived from
    them), since logging the PII into the audit trail while purporting to
    erase it would defeat the entire point. The ``action`` string
    (``"order.pii_erased"``) plus the entry's own ``target_id``
    (the Order's id) is enough for an audit trail to answer "who erased
    this order's buyer details, and when" without retaining what was
    erased.

    Does not commit — the caller (the erase-pii API route) controls the
    transaction boundary, matching every other mutating function in
    ``app.services``.

    Caller-visible consequence worth knowing about: any downstream action
    that depends on ``buyer_email`` being a real, deliverable address —
    concretely, the admin "resend confirmation email" action
    (``app.api.routes.orders.resend_confirmation_email``) — will, after
    this runs, attempt to send to the ``*.invalid`` placeholder address and
    fail. That is expected/correct behavior for an erased order (there is
    no real address left to send to), not a bug to work around here.
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

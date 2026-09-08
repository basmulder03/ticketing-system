"""Door-scanning validation and entry-completion logic (Milestone 7): the
service layer behind ``POST /api/v1/shows/{show_id}/scan``, callable
directly (and thus unit-testable) independent of the route/HTTP layer, per
this codebase's established split between ``app.services.*`` and
``app.api.routes.*``.

Outcome semantics (see ``app.models.enums.ScanOutcome`` for the exact enum
and per-value docstrings) — checked in this fixed order, each one a
terminal result:

1. ``INVALID`` — the token's HMAC signature doesn't verify, or verifies but
   names a ``Ticket.id`` that doesn't exist. Both cases return the exact
   same generic message so a caller can never distinguish "tampered
   signature" from "well-formed but unknown ticket" — see
   ``app.core.qr_tokens.verify_ticket_token``.
2. ``WRONG_SHOW`` — the ticket is real but its ``TicketType.show_id``
   doesn't match the show being scanned for.
3. ``ALREADY_SCANNED`` — the ticket belongs to the right show but
   ``scanned_at`` is already set.
4. ``UNPAID`` — the ticket is real, right show, unscanned, but its Order
   isn't ``paid``. Any non-``paid`` status hits this branch (not just
   ``pending_door``) — deliberately not special-cased, mirroring how
   ``app.services.order_payment.mark_order_paid`` treats "any non-paid
   status" uniformly.
5. ``PASS`` — everything checks out. This is the only branch that mutates
   the Ticket (``scanned_at``/``scanned_by``) — entry is only ever
   completed here.

**Design decision — the unpaid -> resolve -> complete-entry flow (flagged
for review):** scanning an unpaid ticket does NOT mark it scanned, per the
brief ("does not silently let someone through... before the entry is
treated as complete"). This module deliberately does NOT add a second,
distinct "complete entry now that it's paid" action. Instead, the flow is:
the frontend shows the ``UNPAID`` result (order id + amount due) to an
ADMIN-role user standing at the same scan screen; that admin triggers the
EXISTING, unchanged ``POST /api/v1/orders/{order_id}/mark-paid`` route
(``app.api.routes.orders.mark_paid``) to settle payment; staff then
re-scans the SAME QR code, which now naturally falls through to ``PASS``
and completes entry. This was chosen over adding a new
"complete-entry-after-payment" endpoint because (a) it needs zero new
payment-adjacent surface for a scanner-role principal — the scoping
principle in this milestone's brief is explicit that scanner accounts
must not gain any ability to touch payments, and a distinct
"complete entry" action callable right after marking paid would sit
exactly on that boundary; (b) re-scanning is a real, physical action door
staff already do for every other ticket, so it doesn't add a new UI
concept, just a natural repeat of the same action once payment is
resolved; and (c) it keeps this module's only Ticket-mutating code path
to one place (the ``PASS`` branch), which is also where the
"not already scanned" row lock already lives — no second mutating path to
keep in sync with it.

Concurrency discipline (the safety-critical part, this milestone's
equivalent of ``app.services.stock.reserve_stock``): the Ticket row is
resolved from the verified token id and then read via a single
``SELECT ... FOR UPDATE`` query — the FIRST and ONLY read of that row in
this session. Structuring it this way (rather than a plain lookup followed
by a second locking query) means the SQLAlchemy identity-map staleness bug
documented at length in ``app.services.order_payment._lock_order`` and
``app.services.stock.reserve_stock`` (a locked row silently returning a
stale, earlier-cached Python object without
``execution_options(populate_existing=True)``) cannot occur here at all —
there is no earlier plain read of the same Ticket to go stale. If a future
change to this module ever needs an earlier plain read of the same Ticket
row for any reason, ``populate_existing=True`` MUST be added to this
query, matching that precedent.

The row lock is scoped to the Ticket table only — the related TicketType/
Show/Event/Order rows loaded alongside it are plain (non-locking) reads.
That's intentional: the only race this milestone's brief calls out as
safety-critical is "not already scanned" (two concurrent scans of the same
physical ticket), which is fully guarded by locking the Ticket row before
checking/setting ``scanned_at``. A concurrent Order status change (e.g. an
admin marking the order paid in the same instant as a scan) racing against
the ``UNPAID``/``PASS`` read is not a correctness bug: at worst a scan
momentarily sees the pre-transition status, and the next scan (or the same
one retried) sees the current one — unlike overselling or double-entry,
there's no way for this race to admit two people on one ticket or lose a
payment record.

Every outcome is audited (see :func:`_audit`) under one consistent action
name, ``ticket.scan``, with a ``result`` field in ``detail`` set to the
``ScanOutcome`` value — chosen over five separate action names so the
audit log stays greppable by a single action ("every scan attempt, ever")
while remaining fully segmentable by outcome via that field, matching how
``app.services.order_payment.mark_order_paid`` uses one action
(``order.mark_paid``) rather than a name per starting status.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import Principal
from app.core.qr_tokens import verify_ticket_token
from app.models.admin_user import AdminUser
from app.models.enums import OrderStatus, ScanOutcome
from app.models.show import Show
from app.models.ticket import Ticket
from app.models.ticket_type import TicketType
from app.services.audit import record_audit_entry

__all__ = ["ScanResult", "scan_ticket"]


@dataclass(frozen=True)
class ScanResult:
    """Result of one :func:`scan_ticket` call — see
    ``app.schemas.scan.ScanResponse`` (the route maps one directly onto the
    other) for what each field means to an API caller."""

    outcome: ScanOutcome
    message: str
    ticket_id: uuid.UUID | None = None
    ticket_type_name: str | None = None
    buyer_name: str | None = None
    order_id: uuid.UUID | None = None
    order_status: OrderStatus | None = None
    amount_due: Decimal | None = None
    scanned_at: datetime | None = None
    scanned_by_name: str | None = None
    actual_show_id: uuid.UUID | None = None
    actual_show_label: str | None = None


_GENERIC_INVALID_MESSAGE = "Invalid or unrecognized ticket code."


async def _audit(
    session: AsyncSession,
    principal: Principal,
    *,
    outcome: ScanOutcome,
    show_id: uuid.UUID,
    detail: dict[str, Any] | None = None,
) -> None:
    """Write one ``ticket.scan`` audit entry — see this module's docstring
    for the action-naming scheme. Always includes ``result``/``show_id``;
    callers pass anything outcome-specific (ticket id, order id, the wrong
    show's id, etc.) via ``detail``."""
    full_detail: dict[str, Any] = {"result": outcome.value, "show_id": str(show_id)}
    if detail:
        full_detail.update(detail)
    await record_audit_entry(
        session,
        principal,
        action="ticket.scan",
        target_type="Ticket",
        target_id=detail.get("ticket_id") if detail else None,
        detail=full_detail,
    )


async def scan_ticket(
    session: AsyncSession,
    *,
    token: str,
    show_id: uuid.UUID,
    principal: Principal,
) -> ScanResult:
    """Validate and (on a genuine pass) complete entry for the ticket
    encoded in ``token``, scoped to ``show_id``.

    See this module's docstring for the full outcome ordering, the
    unpaid -> resolve -> re-scan flow, and the concurrency discipline. Every
    branch writes exactly one audit entry before returning. Commits its own
    transaction on every branch (there is exactly one caller — the scan
    route — with no further work to combine into the same transaction,
    unlike e.g. ``app.services.order_payment.mark_order_paid``, which
    deliberately leaves committing to its multiple callers).
    """
    ticket_id = verify_ticket_token(token)
    if ticket_id is None:
        await _audit(session, principal, outcome=ScanOutcome.INVALID, show_id=show_id, detail={"reason": "bad_signature"})
        await session.commit()
        return ScanResult(outcome=ScanOutcome.INVALID, message=_GENERIC_INVALID_MESSAGE)

    # Safety-critical row lock: the FIRST and ONLY read of this Ticket row
    # in this session — see this module's docstring for why that ordering
    # is what makes this immune to the identity-map staleness bug class
    # documented in app.services.stock/order_payment, without needing
    # `populate_existing=True`.
    result = await session.execute(
        select(Ticket)
        .where(Ticket.id == ticket_id)
        .options(
            selectinload(Ticket.order),
            selectinload(Ticket.ticket_type).selectinload(TicketType.show).selectinload(Show.event),
        )
        .with_for_update()
    )
    ticket = result.scalar_one_or_none()
    if ticket is None:
        await _audit(
            session,
            principal,
            outcome=ScanOutcome.INVALID,
            show_id=show_id,
            detail={"reason": "unknown_ticket", "ticket_id": str(ticket_id)},
        )
        await session.commit()
        return ScanResult(outcome=ScanOutcome.INVALID, message=_GENERIC_INVALID_MESSAGE)

    ticket_type = ticket.ticket_type
    show = ticket_type.show
    order = ticket.order

    if show.id != show_id:
        event = show.event
        label = f"{event.name} — {show.date.isoformat()}" if event is not None else show.date.isoformat()
        await _audit(
            session,
            principal,
            outcome=ScanOutcome.WRONG_SHOW,
            show_id=show_id,
            detail={"ticket_id": str(ticket.id), "actual_show_id": str(show.id)},
        )
        await session.commit()
        return ScanResult(
            outcome=ScanOutcome.WRONG_SHOW,
            message=f"This ticket is for a different show: {label}.",
            ticket_id=ticket.id,
            actual_show_id=show.id,
            actual_show_label=label,
        )

    if ticket.scanned_at is not None:
        scanned_by_name: str | None = None
        if ticket.scanned_by is not None:
            scanner = await session.get(AdminUser, ticket.scanned_by)
            scanned_by_name = scanner.email if scanner is not None else None
        await _audit(
            session,
            principal,
            outcome=ScanOutcome.ALREADY_SCANNED,
            show_id=show_id,
            detail={
                "ticket_id": str(ticket.id),
                "originally_scanned_at": ticket.scanned_at.isoformat(),
                "originally_scanned_by": str(ticket.scanned_by) if ticket.scanned_by else None,
            },
        )
        await session.commit()
        return ScanResult(
            outcome=ScanOutcome.ALREADY_SCANNED,
            message="This ticket has already been scanned.",
            ticket_id=ticket.id,
            ticket_type_name=ticket_type.name,
            buyer_name=order.buyer_name if order is not None else None,
            scanned_at=ticket.scanned_at,
            scanned_by_name=scanned_by_name,
        )

    if order is None or order.status != OrderStatus.PAID:
        await _audit(
            session,
            principal,
            outcome=ScanOutcome.UNPAID,
            show_id=show_id,
            detail={
                "ticket_id": str(ticket.id),
                "order_id": str(order.id) if order is not None else None,
                "order_status": order.status.value if order is not None else None,
            },
        )
        await session.commit()
        return ScanResult(
            outcome=ScanOutcome.UNPAID,
            message="NOT PAID — collect payment before entry.",
            ticket_id=ticket.id,
            ticket_type_name=ticket_type.name,
            buyer_name=order.buyer_name if order is not None else None,
            order_id=order.id if order is not None else None,
            order_status=order.status if order is not None else None,
            amount_due=order.total if order is not None else None,
        )

    ticket.scanned_at = datetime.now(UTC)
    ticket.scanned_by = principal.id
    await _audit(
        session,
        principal,
        outcome=ScanOutcome.PASS,
        show_id=show_id,
        detail={"ticket_id": str(ticket.id), "order_id": str(order.id)},
    )
    await session.commit()
    return ScanResult(
        outcome=ScanOutcome.PASS,
        message="Entry complete.",
        ticket_id=ticket.id,
        ticket_type_name=ticket_type.name,
        buyer_name=order.buyer_name,
        order_id=order.id,
        order_status=order.status,
        scanned_at=ticket.scanned_at,
        scanned_by_name=principal.name,
    )

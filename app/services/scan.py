"""Ticket scanning: validate a QR token for a show and complete entry.

Outcomes, checked in order (each terminal): ``INVALID`` (bad signature or
unknown ticket — same message for both), ``WRONG_SHOW``, ``ALREADY_SCANNED``,
``UNPAID`` (any non-paid status), ``PASS`` — the only branch that marks the
ticket scanned.

Unpaid flow: an unpaid scan doesn't admit anyone. An admin settles it via the
normal mark-as-paid route, then the same QR code is rescanned and passes.
That keeps scanner accounts away from payments and keeps one mutating path.

Concurrency: the Ticket is read *once*, with ``FOR UPDATE``, so two scans of
one ticket can't both pass, and there's no earlier read to go stale (add
``populate_existing=True`` if you ever add one). Only the ticket is locked; a
racing payment change can at worst need a rescan.

Every attempt is audited as ``ticket.scan`` with ``detail.result``.
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
    """One scan's result; mapped 1:1 onto ``ScanResponse``."""

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
    """Write one ``ticket.scan`` entry with ``result``, ``show_id`` and any extra ``detail``."""
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
    """Validate ``token`` for ``show_id`` and complete entry on a pass.
    Writes one audit entry per call and commits its own transaction.
    """
    ticket_id = verify_ticket_token(token)
    if ticket_id is None:
        await _audit(session, principal, outcome=ScanOutcome.INVALID, show_id=show_id, detail={"reason": "bad_signature"})
        await session.commit()
        return ScanResult(outcome=ScanOutcome.INVALID, message=_GENERIC_INVALID_MESSAGE)

    # Safety-critical: the first and only read of this ticket, locked — see
    # the module docstring.
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

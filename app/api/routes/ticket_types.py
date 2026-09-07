"""TicketType CRUD routes, nested under a Show.

Content-type data per PROJECT_BRIEF.md's AI/Agent Access section — gated by
``require_admin_or_agent``, same as Event/Show.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, require_admin_or_agent
from app.api.routes._utils import apply_partial_update, parse_uuid_or_404
from app.db.session import get_session
from app.models.show import Show
from app.models.ticket_type import TicketType
from app.schemas.ticket_type import TicketTypeCreateRequest, TicketTypeOut, TicketTypeUpdateRequest
from app.services.audit import record_audit_entry

router = APIRouter(prefix="/api/v1/shows/{show_id}/ticket-types", tags=["ticket-types"])


def _to_out(ticket_type: TicketType) -> TicketTypeOut:
    return TicketTypeOut(
        id=str(ticket_type.id),
        show_id=str(ticket_type.show_id),
        name=ticket_type.name,
        price=ticket_type.price,
        service_fee_included=ticket_type.service_fee_included,
        quantity_available=ticket_type.quantity_available,
        remaining=ticket_type.remaining,
        created_at=ticket_type.created_at,
        updated_at=ticket_type.updated_at,
    )


async def _get_show_or_404(session: AsyncSession, show_id: str) -> Show:
    parsed_id = parse_uuid_or_404(show_id, detail="Show not found.")
    show = await session.get(Show, parsed_id)
    if show is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Show not found.")
    return show


async def _get_ticket_type_or_404(session: AsyncSession, show: Show, ticket_type_id: str) -> TicketType:
    parsed_id = parse_uuid_or_404(ticket_type_id, detail="Ticket type not found.")
    ticket_type = await session.get(TicketType, parsed_id)
    if ticket_type is None or ticket_type.show_id != show.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket type not found.")
    return ticket_type


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_ticket_type(
    show_id: str,
    body: TicketTypeCreateRequest,
    principal: Principal = Depends(require_admin_or_agent),
    session: AsyncSession = Depends(get_session),
) -> TicketTypeOut:
    """Create a new TicketType under the given Show."""
    show = await _get_show_or_404(session, show_id)

    ticket_type = TicketType(
        show_id=show.id,
        name=body.name,
        price=body.price,
        service_fee_included=body.service_fee_included,
        quantity_available=body.quantity_available,
    )
    session.add(ticket_type)
    await session.flush()
    await record_audit_entry(
        session,
        principal,
        action="ticket_type.create",
        target_type="TicketType",
        target_id=str(ticket_type.id),
        detail={"show_id": str(show.id), "name": ticket_type.name, "price": str(ticket_type.price)},
    )
    await session.commit()
    return _to_out(ticket_type)


@router.get("")
async def list_ticket_types(
    show_id: str,
    principal: Principal = Depends(require_admin_or_agent),
    session: AsyncSession = Depends(get_session),
) -> list[TicketTypeOut]:
    """List all ticket types under the given Show."""
    show = await _get_show_or_404(session, show_id)
    result = await session.execute(
        select(TicketType).where(TicketType.show_id == show.id).order_by(TicketType.created_at)
    )
    return [_to_out(ticket_type) for ticket_type in result.scalars().all()]


@router.get("/{ticket_type_id}")
async def get_ticket_type(
    show_id: str,
    ticket_type_id: str,
    principal: Principal = Depends(require_admin_or_agent),
    session: AsyncSession = Depends(get_session),
) -> TicketTypeOut:
    """Fetch a single TicketType by id, scoped to its parent Show."""
    show = await _get_show_or_404(session, show_id)
    ticket_type = await _get_ticket_type_or_404(session, show, ticket_type_id)
    return _to_out(ticket_type)


@router.patch("/{ticket_type_id}")
async def update_ticket_type(
    show_id: str,
    ticket_type_id: str,
    body: TicketTypeUpdateRequest,
    principal: Principal = Depends(require_admin_or_agent),
    session: AsyncSession = Depends(get_session),
) -> TicketTypeOut:
    """Partially update a TicketType. Only fields present in the body are changed."""
    show = await _get_show_or_404(session, show_id)
    ticket_type = await _get_ticket_type_or_404(session, show, ticket_type_id)

    changes = apply_partial_update(ticket_type, body)
    await record_audit_entry(
        session,
        principal,
        action="ticket_type.update",
        target_type="TicketType",
        target_id=str(ticket_type.id),
        detail={k: str(v) for k, v in changes.items()},
    )
    await session.commit()
    await session.refresh(ticket_type)
    return _to_out(ticket_type)


@router.delete("/{ticket_type_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ticket_type(
    show_id: str,
    ticket_type_id: str,
    principal: Principal = Depends(require_admin_or_agent),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete a TicketType."""
    show = await _get_show_or_404(session, show_id)
    ticket_type = await _get_ticket_type_or_404(session, show, ticket_type_id)
    await record_audit_entry(
        session,
        principal,
        action="ticket_type.delete",
        target_type="TicketType",
        target_id=str(ticket_type.id),
        detail={"show_id": str(show.id), "name": ticket_type.name},
    )
    await session.delete(ticket_type)
    await session.commit()

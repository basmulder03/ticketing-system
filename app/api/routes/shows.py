"""Show CRUD, nested under an Event. Admin or agent."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, require_admin_or_agent
from app.api.routes._utils import apply_partial_update, commit_or_conflict, parse_uuid_or_404
from app.db.session import get_session
from app.models.enums import PublishStatus
from app.models.event import Event
from app.models.show import Show
from app.models.ticket_type import TicketType
from app.schemas.show import (
    DOORS_AFTER_START_MESSAGE,
    ShowCreateRequest,
    ShowOut,
    ShowUpdateRequest,
)
from app.services.audit import record_audit_entry

router = APIRouter(prefix="/api/v1/events/{event_id}/shows", tags=["shows"])


def _to_out(show: Show) -> ShowOut:
    return ShowOut(
        id=str(show.id),
        event_id=str(show.event_id),
        date=show.date,
        doors_time=show.doors_time,
        start_time=show.start_time,
        venue_name=show.venue_name,
        venue_address=show.venue_address,
        capacity=show.capacity,
        status=show.status,
        created_at=show.created_at,
        updated_at=show.updated_at,
    )


async def _get_event_or_404(session: AsyncSession, event_id: str) -> Event:
    parsed_id = parse_uuid_or_404(event_id, detail="Event not found.")
    event = await session.get(Event, parsed_id)
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found.")
    return event


async def _get_show_or_404(session: AsyncSession, event: Event, show_id: str) -> Show:
    parsed_id = parse_uuid_or_404(show_id, detail="Show not found.")
    show = await session.get(Show, parsed_id)
    if show is None or show.event_id != event.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Show not found.")
    return show


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_show(
    event_id: str,
    body: ShowCreateRequest,
    principal: Principal = Depends(require_admin_or_agent),
    session: AsyncSession = Depends(get_session),
) -> ShowOut:
    """Create a show under the event."""
    event = await _get_event_or_404(session, event_id)

    show = Show(
        event_id=event.id,
        date=body.date,
        doors_time=body.doors_time,
        start_time=body.start_time,
        venue_name=body.venue_name,
        venue_address=body.venue_address,
        capacity=body.capacity,
        status=body.status,
    )
    session.add(show)
    await session.flush()
    await record_audit_entry(
        session,
        principal,
        action="show.create",
        target_type="Show",
        target_id=str(show.id),
        detail={"event_id": str(event.id), "date": str(show.date), "venue_name": show.venue_name},
    )
    await session.commit()
    return _to_out(show)


@router.get("")
async def list_shows(
    event_id: str,
    principal: Principal = Depends(require_admin_or_agent),
    session: AsyncSession = Depends(get_session),
) -> list[ShowOut]:
    """The event's shows, by date."""
    event = await _get_event_or_404(session, event_id)
    result = await session.execute(select(Show).where(Show.event_id == event.id).order_by(Show.date))
    return [_to_out(show) for show in result.scalars().all()]


@router.get("/{show_id}")
async def get_show(
    event_id: str,
    show_id: str,
    principal: Principal = Depends(require_admin_or_agent),
    session: AsyncSession = Depends(get_session),
) -> ShowOut:
    """One show, scoped to its event."""
    event = await _get_event_or_404(session, event_id)
    show = await _get_show_or_404(session, event, show_id)
    return _to_out(show)


@router.post("/{show_id}/duplicate", status_code=status.HTTP_201_CREATED)
async def duplicate_show(
    event_id: str,
    show_id: str,
    principal: Principal = Depends(require_admin_or_agent),
    session: AsyncSession = Depends(get_session),
) -> ShowOut:
    """Copy a show and its ticket types as a starting point for the next
    performance. The copy is always ``draft`` (publishing stays explicit) and
    starts fully available — ticket types store no sales of their own.
    """
    event = await _get_event_or_404(session, event_id)
    source = await _get_show_or_404(session, event, show_id)

    result = await session.execute(select(TicketType).where(TicketType.show_id == source.id))
    source_ticket_types = list(result.scalars().all())

    new_show = Show(
        event_id=event.id,
        date=source.date,
        doors_time=source.doors_time,
        start_time=source.start_time,
        venue_name=source.venue_name,
        venue_address=source.venue_address,
        capacity=source.capacity,
        status=PublishStatus.DRAFT,
    )
    session.add(new_show)
    await session.flush()

    for source_ticket_type in source_ticket_types:
        session.add(
            TicketType(
                show_id=new_show.id,
                name=source_ticket_type.name,
                price=source_ticket_type.price,
                service_fee_included=source_ticket_type.service_fee_included,
                quantity_available=source_ticket_type.quantity_available,
            )
        )

    await record_audit_entry(
        session,
        principal,
        action="show.duplicate",
        target_type="Show",
        target_id=str(new_show.id),
        detail={
            "event_id": str(event.id),
            "source_show_id": str(source.id),
            "ticket_types_copied": len(source_ticket_types),
        },
    )
    await session.commit()
    await session.refresh(new_show)
    return _to_out(new_show)


@router.patch("/{show_id}")
async def update_show(
    event_id: str,
    show_id: str,
    body: ShowUpdateRequest,
    principal: Principal = Depends(require_admin_or_agent),
    session: AsyncSession = Depends(get_session),
) -> ShowOut:
    """PATCH; only fields present are changed."""
    event = await _get_event_or_404(session, event_id)
    show = await _get_show_or_404(session, event, show_id)

    changes = apply_partial_update(show, body)
    if show.doors_time >= show.start_time:
        # Either field alone was valid; only the *pair* (one possibly just-updated,
        # one carried over from the existing row) can conflict.
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=DOORS_AFTER_START_MESSAGE)
    # Stringify values: the audit JSON column can't encode date/time.
    await record_audit_entry(
        session,
        principal,
        action="show.update",
        target_type="Show",
        target_id=str(show.id),
        detail={k: str(v) for k, v in changes.items()},
    )
    await session.commit()
    await session.refresh(show)
    return _to_out(show)


@router.delete("/{show_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_show(
    event_id: str,
    show_id: str,
    principal: Principal = Depends(require_admin_or_agent),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete the show and its ticket types; 409 if any has sold tickets."""
    event = await _get_event_or_404(session, event_id)
    show = await _get_show_or_404(session, event, show_id)
    await record_audit_entry(
        session, principal, action="show.delete", target_type="Show", target_id=str(show.id),
        detail={"event_id": str(event.id), "date": str(show.date)},
    )
    await session.delete(show)
    await commit_or_conflict(session, detail="Cannot delete: this show has ticket types with existing orders.")

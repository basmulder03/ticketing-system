"""Event CRUD routes.

Content-type data per PROJECT_BRIEF.md's AI/Agent Access section — every
route here uses ``require_admin_or_agent`` (both human admins and agent keys
may read/write), never ``require_admin``. EventConfig (SMTP/Mollie
credentials, financial data) is deliberately a separate router
(``app.api.routes.event_configs``) gated by ``require_admin`` only.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, require_admin_or_agent
from app.api.routes._utils import apply_partial_update, commit_or_conflict, parse_uuid_or_404
from app.db.session import get_session
from app.models.event import Event
from app.schemas.event import EventCreateRequest, EventOut, EventUpdateRequest
from app.services.audit import record_audit_entry

router = APIRouter(prefix="/api/v1/events", tags=["events"])


def _to_out(event: Event) -> EventOut:
    return EventOut(
        id=str(event.id),
        name=event.name,
        slug=event.slug,
        description=event.description,
        status=event.status,
        sales_paused=event.sales_paused,
        created_at=event.created_at,
        updated_at=event.updated_at,
    )


async def _get_event_or_404(session: AsyncSession, event_id: str) -> Event:
    parsed_id = parse_uuid_or_404(event_id, detail="Event not found.")
    event = await session.get(Event, parsed_id)
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found.")
    return event


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_event(
    body: EventCreateRequest,
    principal: Principal = Depends(require_admin_or_agent),
    session: AsyncSession = Depends(get_session),
) -> EventOut:
    """Create a new Event. Reachable by admin and agent principals."""
    existing = await session.execute(select(Event).where(Event.slug == body.slug))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="An event with this slug already exists.")

    event = Event(
        name=body.name,
        slug=body.slug,
        description=body.description,
        status=body.status,
        sales_paused=body.sales_paused,
    )
    session.add(event)
    await session.flush()
    await record_audit_entry(
        session,
        principal,
        action="event.create",
        target_type="Event",
        target_id=str(event.id),
        detail={"name": event.name, "slug": event.slug},
    )
    await session.commit()
    return _to_out(event)


@router.get("")
async def list_events(
    principal: Principal = Depends(require_admin_or_agent),
    session: AsyncSession = Depends(get_session),
) -> list[EventOut]:
    """List all events, newest first."""
    result = await session.execute(select(Event).order_by(Event.created_at.desc()))
    return [_to_out(event) for event in result.scalars().all()]


@router.get("/{event_id}")
async def get_event(
    event_id: str,
    principal: Principal = Depends(require_admin_or_agent),
    session: AsyncSession = Depends(get_session),
) -> EventOut:
    """Fetch a single Event by id."""
    event = await _get_event_or_404(session, event_id)
    return _to_out(event)


@router.patch("/{event_id}")
async def update_event(
    event_id: str,
    body: EventUpdateRequest,
    principal: Principal = Depends(require_admin_or_agent),
    session: AsyncSession = Depends(get_session),
) -> EventOut:
    """Partially update an Event. Only fields present in the body are changed."""
    event = await _get_event_or_404(session, event_id)

    if body.slug is not None and body.slug != event.slug:
        existing = await session.execute(select(Event).where(Event.slug == body.slug))
        if existing.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="An event with this slug already exists."
            )

    changes = apply_partial_update(event, body)
    await record_audit_entry(
        session, principal, action="event.update", target_type="Event", target_id=str(event.id), detail=changes
    )
    await session.commit()
    await session.refresh(event)
    return _to_out(event)


@router.delete("/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_event(
    event_id: str,
    principal: Principal = Depends(require_admin_or_agent),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete an Event and everything under it (EventConfig, Shows, TicketTypes cascade).

    Fails with a 409 (not a 500) if any of its TicketTypes still have
    purchased Tickets attached — see ``app.api.routes._utils.commit_or_conflict``.
    """
    event = await _get_event_or_404(session, event_id)
    await record_audit_entry(
        session, principal, action="event.delete", target_type="Event", target_id=str(event.id),
        detail={"name": event.name, "slug": event.slug},
    )
    await session.delete(event)
    await commit_or_conflict(
        session, detail="Cannot delete: this event has ticket types with existing orders."
    )

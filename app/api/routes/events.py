"""Event CRUD. Admin or agent; EventConfig (credentials, finance) lives in
the admin-only ``event_configs`` router.
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
        preview_token=event.preview_token,
        is_default_event=event.is_default_event,
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
    """Create an event."""
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
    """PATCH; only fields present are changed."""
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


@router.post("/{event_id}/set-default")
async def set_default_event(
    event_id: str,
    principal: Principal = Depends(require_admin_or_agent),
    session: AsyncSession = Depends(get_session),
) -> EventOut:
    """Make this the default event (``/`` redirects to it while published).

    Clears any previous default in the same transaction; the partial unique
    index backs this up. Allowed on drafts so it can be set up before
    publishing.
    """
    event = await _get_event_or_404(session, event_id)
    if not event.is_default_event:
        # Lock the current default first so two concurrent requests can't both
        # see "no default"; the partial unique index is the backstop.
        previous_default_result = await session.execute(
            select(Event).where(Event.is_default_event.is_(True)).with_for_update()
        )
        previous_default = previous_default_result.scalar_one_or_none()
        if previous_default is not None:
            previous_default.is_default_event = False
            # Flush now: SQLAlchemy orders same-table UPDATEs by primary
            # key, not mutation order, so without this the new default can
            # hit the unique index before the old one is cleared.
            await session.flush()

        event.is_default_event = True
        await record_audit_entry(
            session,
            principal,
            action="event.set_default",
            target_type="Event",
            target_id=str(event.id),
            detail={"name": event.name, "slug": event.slug},
        )
        await session.commit()
        await session.refresh(event)
    return _to_out(event)


@router.post("/{event_id}/unset-default")
async def unset_default_event(
    event_id: str,
    principal: Principal = Depends(require_admin_or_agent),
    session: AsyncSession = Depends(get_session),
) -> EventOut:
    """Clear this event's default status. Idempotent."""
    event = await _get_event_or_404(session, event_id)
    if event.is_default_event:
        event.is_default_event = False
        await record_audit_entry(
            session,
            principal,
            action="event.unset_default",
            target_type="Event",
            target_id=str(event.id),
            detail={"name": event.name, "slug": event.slug},
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
    """Delete the event and everything under it; 409 if any ticket type has sold tickets."""
    event = await _get_event_or_404(session, event_id)
    await record_audit_entry(
        session, principal, action="event.delete", target_type="Event", target_id=str(event.id),
        detail={"name": event.name, "slug": event.slug},
    )
    await session.delete(event)
    await commit_or_conflict(
        session, detail="Cannot delete: this event has ticket types with existing orders."
    )

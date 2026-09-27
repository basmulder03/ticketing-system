"""Small helpers shared by the route modules."""

import uuid
from typing import Any

from fastapi import HTTPException, status
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


def parse_uuid_or_404(raw: str, *, detail: str) -> uuid.UUID:
    """Parse a UUID, 404ing on a malformed one — same as "not found" to a client."""
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail) from None


def apply_partial_update(instance: Any, body: BaseModel) -> dict[str, Any]:
    """Apply only the fields present in ``body`` (PATCH semantics); return the
    applied changes (handy for audit ``detail``).
    """
    changes = body.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(instance, field, value)
    return changes


async def commit_or_conflict(session: AsyncSession, *, detail: str) -> None:
    """Commit, turning an integrity error into a 409 instead of a 500 — e.g.
    deleting a ticket type that has sold tickets (``RESTRICT``).
    """
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=detail,
        ) from None
